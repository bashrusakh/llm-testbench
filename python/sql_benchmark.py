from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence

import duckdb
import sqlparse

LlmCallback = Callable[..., Awaitable[str]]
ToolLlmCallback = Callable[..., Awaitable[Dict[str, Any]]]

# Wall-clock cap on a single SQL query. A model can emit a runaway query
# (huge cross join, accidental cartesian product) that would otherwise pin a
# CPU and hang the job forever. DuckDB is blocking, so a watchdog thread calls
# connection.interrupt() once the budget is exceeded; the running query then
# raises a duckdb.Error which the existing call-site handlers already catch.
SQL_EXECUTION_TIMEOUT_S = 30.0


class SqlExecutionTimeout(duckdb.Error):
    """Raised when a single query exceeds SQL_EXECUTION_TIMEOUT_S.

    Subclasses ``duckdb.Error`` on purpose: every ``_execute_sql`` call site
    already catches ``duckdb.Error``, so a timeout flows through the normal
    failure/retry paths without any extra handling.
    """

RUN_SQL_QUERY_TOOL = {
    "type": "function",
    "function": {
        "name": "run_sql_query",
        "description": "Execute a SQL query against the DuckDB database. Call this when you have a SQL query ready to run.",
        "parameters": {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "The DuckDB SQL query to execute. ONLY ever pass SQL to this."},
            },
            "required": ["sql"],
        },
    },
}

RESULTS_OK_TOOL = {
    "type": "function",
    "function": {
        "name": "results_ok",
        "description": "Confirm that the query results correctly answer the user question. Call this when the results look correct.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}

BENCHMARK_TOOLS = [RUN_SQL_QUERY_TOOL, RESULTS_OK_TOOL]

# Real happy-path is 2 calls (run_sql_query + results_ok); a legitimate
# error→fix→ok cycle is ~3. 10 leaves room for several corrections (weaker
# models often need a few attempts to fix Binder/Parser errors) while still
# acting as a backstop. Identical repeated SQL is force-stopped earlier via
# dedup (see run_question_tool_calling), so this limit is rarely the thing that
# terminates a healthy run.
MAX_TOOL_CALLS = 10


@dataclass(frozen=True)
class QueryExecutionResult:
    columns: List[str]
    rows: List[tuple[Any, ...]]

    @property
    def row_count(self) -> int:
        return len(self.rows)

    @property
    def first_row(self) -> Optional[Dict[str, Any]]:
        if not self.rows:
            return None
        return {
            column: _to_json_compatible(value)
            for column, value in zip(self.columns, self.rows[0])
        }


class SqlBenchmarkRunner:
    # Match an MSSQL-style bracketed identifier, optionally containing spaces
    # (e.g. [Sales Amount], s.[Fiscal Year]). The lookbehind excludes list
    # indexing (arr[1] — '[' preceded by a word char, ']' or ')'), and the
    # content class (must start with a letter/underscore/space) excludes DuckDB
    # list literals like [1,2,3]. Quoted string literals are skipped separately
    # in _convert_mssql_brackets_to_duckdb.
    _MSSQL_BRACKET_RE = re.compile(r'(?<![\w\])])\[([A-Za-z_ ][\w ]*)\]')

    def __init__(
        self,
        llm_callback: LlmCallback,
        data_dir: str | Path,
        *,
        sql_execution_timeout_s: float = SQL_EXECUTION_TIMEOUT_S,
    ) -> None:
        self.llm_callback = llm_callback
        self.data_dir = Path(data_dir)
        self.questions_path = self.data_dir / "questions.json"
        self.tables_dir = self.data_dir / "assets" / "tables"
        # Per-query wall-clock budget; <=0 disables the watchdog entirely.
        self.sql_execution_timeout_s = sql_execution_timeout_s
        self.connection = duckdb.connect(database=":memory:")
        self._closed = False
        try:
            self.questions = self._load_questions()
            self.questions_by_id = {int(question["id"]): question for question in self.questions}
            self.table_schemas = self._load_tables_into_duckdb()
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        if not self._closed:
            self.connection.close()
            self._closed = True

    def __enter__(self) -> "SqlBenchmarkRunner":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    async def run_all(
        self,
        *,
        model: str,
        provider: str,
        endpoint: str,
        timeout_ms: int,
        question_ids: Optional[Sequence[int]] = None,
        thinking_mode: str = "off",
    ) -> List[Dict[str, Any]]:
        if question_ids is None:
            target_ids = sorted(self.questions_by_id)
        else:
            target_ids = [int(question_id) for question_id in question_ids]
        results: List[Dict[str, Any]] = []
        for question_id in target_ids:
            results.append(
                await self.run_question(
                    question_id=question_id,
                    model=model,
                    provider=provider,
                    endpoint=endpoint,
                    timeout_ms=timeout_ms,
                    thinking_mode=thinking_mode,
                )
            )
        return results

    async def run_question(
        self,
        *,
        question_id: int,
        model: str,
        provider: str,
        endpoint: str,
        timeout_ms: int,
        thinking_mode: str = "off",
    ) -> Dict[str, Any]:
        question = self.questions_by_id.get(int(question_id))
        if question is None:
            raise ValueError(f"Unknown question_id: {question_id}")

        try:
            expected_sql = self.normalize_sql(str(question["sql"]))
            expected_execution = self._execute_sql(expected_sql)
            expected_row_count = _safe_int(question.get("row_count"), expected_execution.row_count)
            expected_columns = [str(column) for column in question.get("columns") or expected_execution.columns]
            expected_first_row = _normalize_mapping(question.get("first_row"))
            prompt = self.build_prompt(question, thinking_mode=thinking_mode)
        except (duckdb.Error, ValueError, TypeError, KeyError) as exc:
            return self._build_failure_result(
                question=question,
                model=model,
                provider=provider,
                endpoint=endpoint,
                expected_sql=str(question.get("sql", "")),
                expected_row_count=_safe_optional_int(question.get("row_count")),
                expected_columns=[str(column) for column in question.get("columns") or []],
                expected_first_row=_normalize_mapping(question.get("first_row")) if isinstance(question.get("first_row"), dict) else None,
                generated_sql="",
                error=f"Benchmark setup failed: {exc}",
            
                thinking_mode=thinking_mode,)

        try:
            raw_generated_sql = await self.llm_callback(
                prompt[0],
                prompt[1],
                model=model,
                provider=provider,
                endpoint=endpoint,
                timeout_ms=timeout_ms,
            )
        except Exception as exc:
            return self._build_failure_result(
                question=question,
                model=model,
                provider=provider,
                endpoint=endpoint,
                expected_sql=expected_sql,
                expected_row_count=expected_row_count,
                expected_columns=expected_columns,
                expected_first_row=expected_first_row,
                generated_sql="",
                error=f"LLM callback failed: {type(exc).__name__}: {exc!r}",
            
                thinking_mode=thinking_mode,)

        stripped_sql = self.strip_markdown_fences(raw_generated_sql)
        if not stripped_sql:
            return self._build_failure_result(
                question=question,
                model=model,
                provider=provider,
                endpoint=endpoint,
                expected_sql=expected_sql,
                expected_row_count=expected_row_count,
                expected_columns=expected_columns,
                expected_first_row=expected_first_row,
                generated_sql="",
                error="Generated SQL is empty after cleanup",
            
                thinking_mode=thinking_mode,)

        generated_sql = self.normalize_sql(stripped_sql)
        if not generated_sql:
            return self._build_failure_result(
                question=question,
                model=model,
                provider=provider,
                endpoint=endpoint,
                expected_sql=expected_sql,
                expected_row_count=expected_row_count,
                expected_columns=expected_columns,
                expected_first_row=expected_first_row,
                generated_sql="",
                error="Generated SQL is empty after normalization",
            
                thinking_mode=thinking_mode,)

        try:
            actual_execution = self._execute_sql(generated_sql)
        except (duckdb.Error, ValueError, TypeError) as exc:
            return self._build_failure_result(
                question=question,
                model=model,
                provider=provider,
                endpoint=endpoint,
                expected_sql=expected_sql,
                expected_row_count=expected_row_count,
                expected_columns=expected_columns,
                expected_first_row=expected_first_row,
                generated_sql=generated_sql,
                error=f"Generated SQL execution failed: {exc}",
            
                thinking_mode=thinking_mode,)

        actual_row_count = actual_execution.row_count
        actual_columns = actual_execution.columns
        actual_first_row = actual_execution.first_row

        row_count_match = expected_row_count == actual_row_count
        columns_match = _columns_match(expected_columns, actual_columns)
        first_row_match = True if expected_first_row is None else _values_match(expected_first_row, actual_first_row)
        success = row_count_match and columns_match and first_row_match
        error = "" if success else self._build_mismatch_error(
            row_count_match=row_count_match,
            columns_match=columns_match,
            first_row_match=first_row_match,
        )

        return {
            "benchmark_type": "sql",
            "question_id": int(question["id"]),
            "difficulty": str(question.get("difficulty", "")),
            "question": str(question.get("question", "")),
            "model": model,
            "thinking_mode": thinking_mode,
            "provider": provider,
            "endpoint": endpoint,
            "generated_sql": generated_sql,
            "expected_sql": expected_sql,
            "success": success,
            "outcome": "pass" if success else "fail",
            "error": error,
            "expected_row_count": expected_row_count,
            "actual_row_count": actual_row_count,
            "expected_columns": expected_columns,
            "actual_columns": actual_columns,
            "expected_first_row": expected_first_row,
            "actual_first_row": actual_first_row,
            "row_count_match": row_count_match,
            "columns_match": columns_match,
            "first_row_match": first_row_match,
            "conversation": [],
        }

    def build_prompt(self, question: Dict[str, Any], *, thinking_mode: str = "off") -> tuple[str, str]:
        included_tables = [str(table) for table in question.get("included_tables", [])]
        schema_blocks = [self._schema_block(table_name) for table_name in included_tables]
        schema_text = "\n\n".join(schema_blocks)
        think_line = "Think through the problem step by step before writing the query. " if thinking_mode == "on" else ""
        system = (
            "You are a SQL query generator for DuckDB. "
            "Generate SQL queries that answer the user's questions using DuckDB SQL syntax. "
            "Make sure you quote all field, column and table names. "
            f"{think_line}"
            "\n\nHere is the database schema:\n\n"
            f"{schema_text}"
        )
        user = (
            f"Question ID: {question['id']}\n"
            f"Question: {question.get('question', '')}"
        )
        return system, user

    def build_tool_system_prompt(self, *, thinking_mode: str = "on") -> str:
        all_tables = sorted(self.table_schemas.keys())
        schema_blocks = [self._schema_block(table_name) for table_name in all_tables]
        schema_text = "\n\n".join(schema_blocks)
        think_line = "Think through the problem step by step before writing the query.\n\n" if thinking_mode == "on" else ""
        return (
            "You are a SQL query generator for DuckDB.\n"
            "Generate SQL queries that answer the user's questions using DuckDB SQL syntax.\n"
            "Make sure you quote all field, column and table names.\n"
            f"{think_line}"
            "Here is the database schema:\n\n"
            f"{schema_text}\n\n"
            "You have these tools available: run_sql_query(sql) - Execute a SQL query against the DuckDB database.\n\n"
            "Workflow:\n"
            "- Call run_sql_query with your SQL query. Important: only ever pass SQL to run_sql_query.\n"
            "- If the query returns an error, call run_sql_query with a corrected query.\n"
            "- When results look correct, call results_ok.\n\n"
            "Important rules:\n"
            "- Do NOT call run_sql_query more than once with the same SQL. Repeating an identical query gives the same result and wastes a turn.\n"
            "- Once you have received query results that answer the question, call results_ok immediately. Do not re-run the query to double-check.\n"
            "- Only call run_sql_query again if you are changing the SQL to fix an error or correct the result."
        )

    def _finalize_tool_run(
        self,
        *,
        question: Dict[str, Any],
        model: str,
        provider: str,
        endpoint: str,
        expected_sql: str,
        expected_row_count: int,
        expected_columns: List[str],
        expected_first_row: Optional[Dict[str, Any]],
        last_sql: Optional[str],
        attempts: int,
        total_calls: int,
        input_tokens: int,
        output_tokens: int,
        cost: Optional[float],
        resolved_model: Optional[str],
        messages: List[Dict[str, Any]],
        thinking_mode: str,
        stop_reason: str,
        cached_result: Optional["QueryExecutionResult"] = None,
    ) -> Dict[str, Any]:
        """Validate last_sql and build the final tool result.

        Shared by the results_ok branch and the duplicate-SQL force-stop so both
        paths produce a real, validated result (non-empty generated_sql) instead
        of the model looping until MAX_TOOL_CALLS. `cached_result` lets the
        force-stop reuse an already-computed execution.
        """
        actual_row_count = None
        actual_columns_list = None
        actual_first_row_val = None
        row_count_match = False
        columns_match = False
        fr_match = False
        success = False
        error_msg = ""

        if last_sql:
            try:
                actual_execution = cached_result if cached_result is not None else self._execute_sql(last_sql)
                actual_row_count = actual_execution.row_count
                actual_columns_list = actual_execution.columns
                actual_first_row_val = actual_execution.first_row
                row_count_match = expected_row_count == actual_row_count
                columns_match = _columns_match(expected_columns, actual_columns_list)
                fr_match = True if expected_first_row is None else _values_match(expected_first_row, actual_first_row_val)
                success = row_count_match and columns_match and fr_match
                error_msg = "" if success else self._build_mismatch_error(
                    row_count_match=row_count_match,
                    columns_match=columns_match,
                    first_row_match=fr_match,
                )
            except (duckdb.Error, ValueError, TypeError) as exc:
                error_msg = f"SQL execution/comparison failed: {exc}"

        return self._build_tool_result(
            question=question, model=model, provider=provider,
            endpoint=endpoint, expected_sql=expected_sql,
            expected_row_count=expected_row_count,
            expected_columns=expected_columns,
            expected_first_row=expected_first_row,
            generated_sql=last_sql or "",
            actual_row_count=actual_row_count,
            actual_columns=actual_columns_list,
            actual_first_row=actual_first_row_val,
            row_count_match=row_count_match,
            columns_match=columns_match,
            first_row_match=fr_match,
            success=success, error=error_msg,
            attempts=attempts, tool_calls=total_calls,
            input_tokens=input_tokens, output_tokens=output_tokens,
            cost=cost, resolved_model=resolved_model,
            conversation=list(messages),
            thinking_mode=thinking_mode,
            stop_reason=stop_reason,
        )

    async def run_question_tool_calling(
        self,
        *,
        question_id: int,
        model: str,
        provider: str,
        endpoint: str,
        timeout_ms: int,
        tool_llm_callback: ToolLlmCallback,
        max_retries: int = 5,
        abort_signal: Optional[Any] = None,
        thinking_mode: str = "on",
        question_timeout_ms: int = 0,
        warmed_models: Optional[set] = None,
    ) -> Dict[str, Any]:
        """Run one question via tool-calling.

        question_timeout_ms: per-question wall-clock budget (<=0 disables it).
        Crucially this budget counts only *warm* model time. The first call to a
        model that has not responded yet (cold) runs untimed so that model-load
        time — which can be very long on weak hardware — is never charged against
        the budget. `warmed_models` is a shared set the caller passes across
        questions so a model is treated as cold only on its very first call.
        """
        question = self.questions_by_id.get(int(question_id))
        if question is None:
            raise ValueError(f"Unknown question_id: {question_id}")

        try:
            expected_sql = self.normalize_sql(str(question["sql"]))
            expected_execution = self._execute_sql(expected_sql)
            expected_row_count = _safe_int(question.get("row_count"), expected_execution.row_count)
            expected_columns = [str(column) for column in question.get("columns") or expected_execution.columns]
            expected_first_row = _normalize_mapping(question.get("first_row"))
        except (duckdb.Error, ValueError, TypeError, KeyError) as exc:
            return self._build_failure_result(
                question=question,
                model=model,
                provider=provider,
                endpoint=endpoint,
                expected_sql=str(question.get("sql", "")),
                expected_row_count=_safe_optional_int(question.get("row_count")),
                expected_columns=[str(column) for column in question.get("columns") or []],
                expected_first_row=_normalize_mapping(question.get("first_row")) if isinstance(question.get("first_row"), dict) else None,
                generated_sql="",
                error=f"Benchmark setup failed: {exc}",
                conversation=[],
            
                thinking_mode=thinking_mode,)

        system_prompt = self.build_tool_system_prompt(thinking_mode=thinking_mode)
        user_question = question.get("question", "")
        messages: List[Dict[str, Any]] = [
            {"role": "user", "content": f"Question ID: {question['id']}\nQuestion: {user_question}"}
        ]

        last_sql: Optional[str] = None
        # Maps normalized SQL -> its QueryExecutionResult, for dedup/force-stop.
        executed_sql: Dict[str, "QueryExecutionResult"] = {}
        attempts = 0
        retry_count = 0
        total_calls = 0
        no_tool_call_retries = 0
        # In thinking mode the model outputs <think> blocks before tool calls —
        # that's expected, not a failure. Reduce silent retries so we nudge sooner
        # instead of burning total_calls on identical context replays.
        MAX_NO_TOOL_CALL_RETRIES = 1 if thinking_mode.lower() == "on" else 3
        input_tokens = 0
        output_tokens = 0
        cost: Optional[float] = None
        resolved_model: Optional[str] = None
        error_msg: Optional[str] = None

        # Per-question budget that excludes cold-start model load. The clock is
        # started lazily after the first (cold) model response returns, so load
        # time is never counted. `warmed_models` persists across questions.
        if warmed_models is None:
            warmed_models = set()
        budget_s = question_timeout_ms / 1000.0 if question_timeout_ms and question_timeout_ms > 0 else 0.0
        warm_deadline: Optional[float] = None

        while total_calls < MAX_TOOL_CALLS:
            # Abort check (from original runBenchmark.ts abortSignal)
            if abort_signal is not None:
                if getattr(abort_signal, 'stop_requested', False) or getattr(abort_signal, 'aborted', False) or (callable(abort_signal) and abort_signal()):
                    return self._build_failure_result(
                        question=question, model=model, provider=provider,
                        endpoint=endpoint, expected_sql=expected_sql,
                        expected_row_count=expected_row_count,
                        expected_columns=expected_columns,
                        expected_first_row=expected_first_row,
                        generated_sql=last_sql or "",
                        error="Benchmark run aborted",
                        conversation=list(messages),
                    
                        thinking_mode=thinking_mode,)
            is_cold = model not in warmed_models
            try:
                call_coro = tool_llm_callback(
                    system_prompt=system_prompt,
                    messages=messages,
                    tools=BENCHMARK_TOOLS,
                    model=model,
                    provider=provider,
                    endpoint=endpoint,
                    timeout_ms=timeout_ms,
                )
                if budget_s > 0 and not is_cold:
                    # Warm call: enforce the remaining per-question budget.
                    remaining = budget_s if warm_deadline is None else (warm_deadline - time.monotonic())
                    if remaining <= 0:
                        raise asyncio.TimeoutError()
                    response = await asyncio.wait_for(call_coro, timeout=remaining)
                else:
                    # Cold call (model load) or no budget: run untimed.
                    response = await call_coro
                # Mark the model warm and start the budget clock now that load is done.
                if is_cold:
                    warmed_models.add(model)
                    if budget_s > 0:
                        warm_deadline = time.monotonic() + budget_s
                # Track token usage (from original runBenchmark.ts onTokenUsage)
                usage = response.get("usage", {})
                if usage:
                    input_tokens += usage.get("prompt_tokens", 0)
                    output_tokens += usage.get("completion_tokens", 0)
                # Track resolved model name
                resp_model = response.get("model")
                if resp_model:
                    resolved_model = resp_model
            except asyncio.TimeoutError:
                # Per-question budget exhausted on a warm call. Model load is not
                # counted, so this is genuine inference taking too long.
                return self._build_failure_result(
                    question=question,
                    model=model,
                    provider=provider,
                    endpoint=endpoint,
                    expected_sql=expected_sql,
                    expected_row_count=expected_row_count,
                    expected_columns=expected_columns,
                    expected_first_row=expected_first_row,
                    generated_sql=last_sql or "",
                    error=f"Question exceeded time budget ({question_timeout_ms} ms, excluding model load)",
                    conversation=list(messages),
                    thinking_mode=thinking_mode,
                    stop_reason="question_timeout",)
            except Exception as exc:
                return self._build_failure_result(
                    question=question,
                    model=model,
                    provider=provider,
                    endpoint=endpoint,
                    expected_sql=expected_sql,
                    expected_row_count=expected_row_count,
                    expected_columns=expected_columns,
                    expected_first_row=expected_first_row,
                    generated_sql=last_sql or "",
                    error=f"LLM tool-calling callback failed: {type(exc).__name__}: {exc!r}",
                    conversation=list(messages),

                    thinking_mode=thinking_mode,)

            total_calls += 1
            tool_calls = response.get("tool_calls", [])
            if tool_calls:
                no_tool_call_retries = 0  # reset on successful tool call

            # Handle text-only response: retry up to MAX_NO_TOOL_CALL_RETRIES
            # times (upstream behaviour), then fall back to SQL extraction or follow-up.
            if not tool_calls:
                text_content = response.get("content", "")
                # First: strip thinking tags from raw content
                text_content_clean = self.strip_think_tags(text_content)
                # Check for non-standard tool-call formats (e.g. Gemma <|tool_call>)
                custom_tc = self._parse_custom_tool_call(text_content_clean or text_content)
                if custom_tc:
                    func_name_custom, sql_custom = custom_tc
                    # Build a synthetic tool_calls entry locally (do NOT append to
                    # messages here — the unified executor below appends the
                    # assistant message exactly once and runs the call). Previously
                    # this branch appended the assistant message but an `elif …
                    # continue` further down skipped the executor entirely, so the
                    # SQL was never run: the model got no tool result and looped
                    # until MAX_TOOL_CALLS (empty generated_sql). See plan §3/A.
                    fake_tc_id = f"custom_{total_calls}"
                    if func_name_custom == "run_sql_query" and sql_custom:
                        tool_calls = [{
                            "id": fake_tc_id,
                            "type": "function",
                            "function": {"name": "run_sql_query", "arguments": json.dumps({"sql": sql_custom})},
                        }]
                        no_tool_call_retries = 0
                    elif func_name_custom == "results_ok":
                        tool_calls = [{
                            "id": fake_tc_id,
                            "type": "function",
                            "function": {"name": "results_ok", "arguments": "{}"},
                        }]
                        no_tool_call_retries = 0

            # Only run the text-extraction / retry fallbacks when we still have no
            # tool calls (native or custom-parsed). A populated tool_calls list
            # falls through to the unified executor below.
            if not tool_calls:
                extracted = self.strip_markdown_fences(text_content_clean or text_content)
                if extracted and self._looks_like_sql(extracted):
                    last_sql = self.normalize_sql(extracted)
                    attempts += 1
                    try:
                        actual_execution = self._execute_sql(last_sql)
                    except (duckdb.Error, ValueError, TypeError) as exc:
                        retry_count += 1
                        messages.append({
                            "role": "assistant",
                            "content": text_content_clean or text_content or "",
                        })
                        messages.append({
                            "role": "tool",
                            "tool_call_id": "text_fallback",
                            "content": f"Error executing query. Fix this error and call run_sql_query again. Error: {exc}",
                        })
                        if retry_count > max_retries:
                            return self._build_failure_result(
                                question=question,
                                model=model,
                                provider=provider,
                                endpoint=endpoint,
                                expected_sql=expected_sql,
                                expected_row_count=expected_row_count,
                                expected_columns=expected_columns,
                                expected_first_row=expected_first_row,
                                generated_sql=last_sql,
                                error=f"Generated SQL execution failed after {max_retries} retries: {exc}",
                                conversation=list(messages),

                                thinking_mode=thinking_mode,)
                        continue
                    actual_row_count = actual_execution.row_count
                    actual_columns = actual_execution.columns
                    actual_first_row = actual_execution.first_row
                    row_count_match = expected_row_count == actual_row_count
                    columns_match = _columns_match(expected_columns, actual_columns)
                    fr_match = True if expected_first_row is None else _values_match(expected_first_row, actual_first_row)
                    success = row_count_match and columns_match and fr_match
                    error_msg = "" if success else self._build_mismatch_error(
                        row_count_match=row_count_match,
                        columns_match=columns_match,
                        first_row_match=fr_match,
                    )
                    return self._build_tool_result(
                        question=question, model=model, provider=provider,
                        endpoint=endpoint, expected_sql=expected_sql,
                        expected_row_count=expected_row_count,
                        expected_columns=expected_columns,
                        expected_first_row=expected_first_row,
                        generated_sql=last_sql,
                        actual_row_count=actual_row_count,
                        actual_columns=actual_columns,
                        actual_first_row=actual_first_row,
                        row_count_match=row_count_match,
                        columns_match=columns_match,
                        first_row_match=fr_match,
                        success=success, error=error_msg,
                        attempts=attempts, tool_calls=total_calls,
                        input_tokens=input_tokens, output_tokens=output_tokens,
                        cost=cost, resolved_model=resolved_model,
                        conversation=list(messages),

                        thinking_mode=thinking_mode,)
                elif last_sql and last_sql in executed_sql:
                    # The model produced prose (no new SQL, no results_ok) AFTER a
                    # query that already executed successfully — e.g. "The query
                    # executed successfully and returned the expected columns…".
                    # Treat this as an implicit results_ok and finalize on the last
                    # good SQL instead of nudging it to run again, which otherwise
                    # loops until MAX_TOOL_CALLS and discards a correct result.
                    messages.append({"role": "assistant", "content": text_content_clean or text_content or ""})
                    return self._finalize_tool_run(
                        question=question, model=model, provider=provider,
                        endpoint=endpoint, expected_sql=expected_sql,
                        expected_row_count=expected_row_count,
                        expected_columns=expected_columns,
                        expected_first_row=expected_first_row,
                        last_sql=last_sql, attempts=attempts, total_calls=total_calls,
                        input_tokens=input_tokens, output_tokens=output_tokens,
                        cost=cost, resolved_model=resolved_model,
                        messages=messages, thinking_mode=thinking_mode,
                        stop_reason="text_implicit_ok",
                        cached_result=executed_sql.get(last_sql),
                    )
                elif no_tool_call_retries < MAX_NO_TOOL_CALL_RETRIES:
                    # Retry same request without adding messages (upstream behaviour)
                    no_tool_call_retries += 1
                    continue
                else:
                    # Retries exhausted: nudge the model with a follow-up
                    no_tool_call_retries = 0
                    messages.append({"role": "assistant", "content": text_content_clean or text_content or ""})
                    messages.append({"role": "user", "content": "Please use the run_sql_query tool to execute your SQL query."})
                    continue

            for tool_call in tool_calls:
                tc_id = tool_call.get("id", "")
                function = tool_call.get("function", {})
                func_name = function.get("name", "")
                arguments_str = function.get("arguments", "{}")

                try:
                    arguments = json.loads(arguments_str) if isinstance(arguments_str, str) else arguments_str
                except json.JSONDecodeError:
                    arguments = {}

                messages.append({
                    "role": "assistant",
                    "content": None,   # OpenAI spec: null when tool_calls present
                    "tool_calls": [{
                        "id": tc_id,
                        "type": "function",
                        "function": {"name": func_name, "arguments": arguments_str},
                    }]
                })

                if func_name == "results_ok":
                    return self._finalize_tool_run(
                        question=question, model=model, provider=provider,
                        endpoint=endpoint, expected_sql=expected_sql,
                        expected_row_count=expected_row_count,
                        expected_columns=expected_columns,
                        expected_first_row=expected_first_row,
                        last_sql=last_sql, attempts=attempts, total_calls=total_calls,
                        input_tokens=input_tokens, output_tokens=output_tokens,
                        cost=cost, resolved_model=resolved_model,
                        messages=messages, thinking_mode=thinking_mode,
                        stop_reason="results_ok",
                    )

                if func_name == "run_sql_query":
                    sql = arguments.get("sql", "")
                    last_sql = self.normalize_sql(sql) if sql else None
                    attempts += 1

                    # Dedup / force-stop: if the model re-issues a SQL we already
                    # executed successfully, do not run it again. Re-running gives
                    # the same result and is the signature of the loop that ate
                    # MAX_TOOL_CALLS (plan §3/B). Instead finalize on that result
                    # exactly as if the model had called results_ok.
                    norm_key = last_sql or ""
                    if norm_key and norm_key in executed_sql:
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "content": "Duplicate SQL — reusing previous result. If the result matches the request, call results_ok. If not, call run_sql_query with a corrected query.",
                        })
                        return self._finalize_tool_run(
                            question=question, model=model, provider=provider,
                            endpoint=endpoint, expected_sql=expected_sql,
                            expected_row_count=expected_row_count,
                            expected_columns=expected_columns,
                            expected_first_row=expected_first_row,
                            last_sql=last_sql, attempts=attempts, total_calls=total_calls,
                            input_tokens=input_tokens, output_tokens=output_tokens,
                            cost=cost, resolved_model=resolved_model,
                            messages=messages, thinking_mode=thinking_mode,
                            stop_reason="duplicate_sql_forced_ok",
                            cached_result=executed_sql.get(norm_key),
                        )

                    try:
                        result = self._execute_sql(last_sql)
                        if norm_key:
                            executed_sql[norm_key] = result
                        result_summary = self._build_result_summary(result)
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "content": result_summary,
                        })
                        retry_count = 0
                    except (duckdb.Error, ValueError, TypeError) as exc:
                        retry_count += 1
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "content": f"Error executing query. Fix this error and call run_sql_query again. Error: {exc}",
                        })
                        if retry_count > max_retries:
                            return self._build_failure_result(
                                question=question,
                                model=model,
                                provider=provider,
                                endpoint=endpoint,
                                expected_sql=expected_sql,
                                expected_row_count=expected_row_count,
                                expected_columns=expected_columns,
                                expected_first_row=expected_first_row,
                                generated_sql=last_sql or "",
                                error=f"Query failed after {max_retries} retries: {exc}",
                                conversation=list(messages),
                            
                                thinking_mode=thinking_mode,)
                else:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": f"Unknown tool '{func_name}'. Available tools: run_sql_query, results_ok",
                    })

        # Hit the tool-call limit. If the model did produce at least one SQL that
        # executed successfully, score that last good query instead of recording
        # a bare error — the model just never said results_ok. Same finalization
        # path as results_ok / text_implicit_ok / duplicate_sql_forced_ok.
        if last_sql and last_sql in executed_sql:
            return self._finalize_tool_run(
                question=question, model=model, provider=provider,
                endpoint=endpoint, expected_sql=expected_sql,
                expected_row_count=expected_row_count,
                expected_columns=expected_columns,
                expected_first_row=expected_first_row,
                last_sql=last_sql, attempts=attempts, total_calls=total_calls,
                input_tokens=input_tokens, output_tokens=output_tokens,
                cost=cost, resolved_model=resolved_model,
                messages=messages, thinking_mode=thinking_mode,
                stop_reason="limit_forced_ok",
                cached_result=executed_sql.get(last_sql),
            )

        return self._build_failure_result(
            question=question,
            model=model,
            provider=provider,
            endpoint=endpoint,
            expected_sql=expected_sql,
            expected_row_count=expected_row_count,
            expected_columns=expected_columns,
            expected_first_row=expected_first_row,
            generated_sql=last_sql or "",
            error=f"Exceeded maximum tool calls ({MAX_TOOL_CALLS})",
            conversation=list(messages),
            thinking_mode=thinking_mode,
            stop_reason="tool_call_limit",)

    async def run_all_tool_calling(
        self,
        *,
        model: str,
        provider: str,
        endpoint: str,
        timeout_ms: int,
        tool_llm_callback: ToolLlmCallback,
        question_ids: Optional[Sequence[int]] = None,
        thinking_mode: str = "on",
    ) -> List[Dict[str, Any]]:
        if question_ids is None:
            target_ids = sorted(self.questions_by_id)
        else:
            target_ids = [int(question_id) for question_id in question_ids]
        results: List[Dict[str, Any]] = []
        for question_id in target_ids:
            results.append(
                await self.run_question_tool_calling(
                    question_id=question_id,
                    model=model,
                    provider=provider,
                    endpoint=endpoint,
                    timeout_ms=timeout_ms,
                    tool_llm_callback=tool_llm_callback,
                    thinking_mode=thinking_mode,
                )
            )
        return results

    @staticmethod
    def _looks_like_sql(text: str) -> bool:
        """Heuristic check if text looks like a SQL SELECT statement."""
        if "..." in text:
            return False
        for line in text.strip().upper().splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("--"):
                continue
            return stripped.startswith("SELECT") or stripped.startswith("WITH")
        return False

    @staticmethod
    def _build_result_summary(result: QueryExecutionResult) -> str:
        """Build result summary for tool-calling feedback (from original prompt.ts)."""
        if not result.rows:
            first_row_str = "(empty)"
        else:
            first_row_vals = ", ".join(
                f"{col}={val}" for col, val in zip(result.columns, result.rows[0])
            )
            first_row_str = first_row_vals
        return (
            f"Query executed successfully. Verify these results match the request.\n\n"
            f"If they do, call results_ok.\n"
            f"If they do not, call run_sql_query with a corrected query.\n\n"
            f"Returned {result.row_count} row(s).\n"
            f"Columns: {', '.join(result.columns)}\n"
            f"First row: {first_row_str}"
        )

    def _build_tool_result(
        self,
        *,
        question: Dict[str, Any],
        model: str,
        provider: str,
        endpoint: str,
        expected_sql: str,
        expected_row_count: int,
        expected_columns: List[str],
        expected_first_row: Optional[Dict[str, Any]],
        generated_sql: str,
        actual_row_count: Optional[int],
        actual_columns: Optional[List[str]],
        actual_first_row: Optional[Dict[str, Any]],
        row_count_match: bool,
        columns_match: bool,
        first_row_match: bool,
        success: bool,
        error: str,
        attempts: int,
        tool_calls: int,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost: Optional[float] = None,
        resolved_model: Optional[str] = None,
        conversation: Optional[List[Dict[str, Any]]] = None,
        thinking_mode: str = "on",
        stop_reason: str = "results_ok",
    ) -> Dict[str, Any]:
        return {
            "benchmark_type": "sql",
            "question_id": int(question["id"]),
            "difficulty": str(question.get("difficulty", "")),
            "question": str(question.get("question", "")),
            "model": resolved_model or model,
            "thinking_mode": thinking_mode,
            "provider": provider,
            "endpoint": endpoint,
            "generated_sql": generated_sql,
            "expected_sql": expected_sql,
            "success": success,
            "outcome": "pass" if success else "fail",
            "error": error,
            "expected_row_count": expected_row_count,
            "actual_row_count": actual_row_count,
            "expected_columns": expected_columns,
            "actual_columns": actual_columns,
            "expected_first_row": expected_first_row,
            "actual_first_row": actual_first_row,
            "row_count_match": row_count_match,
            "columns_match": columns_match,
            "first_row_match": first_row_match,
            "attempts": attempts,
            "tool_calls": tool_calls,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost": cost,
            "stop_reason": stop_reason,
            "first_row_diffs": _compute_first_row_diffs(expected_first_row, actual_first_row) if expected_first_row and actual_first_row else [],
            "conversation": conversation if conversation is not None else [],
        }

    @staticmethod
    def _parse_custom_tool_call(text: str) -> Optional[tuple[str, str]]:
        """Parse non-standard tool-call formats that some models produce.

        Supported formats:

        Pipe-prefixed (call: syntax):
          call:run_sql_query(sql="...")           — standard parenthesised
          call:duckdb:run_sql_query(sql='...')    — duckdb: prefix, single quotes
          call:run_sql_query{sql:"..."}           — JSON-ish braces
          call:results_ok{}                        — confirmation tool

        Gemma-style XML:
          <tool_call>
            <function=run_sql_query>
              <parameter=sql>...SQL...</parameter>
            </function>
          </tool_call>
          <tool_call>
            <function=results_ok></function>
          </tool_call>

        Returns (function_name, sql) or None. For results_ok, sql is "".
        """
        # Try pipe-prefixed format: <|tool_call ... call:function(args)
        start_tag = "<|tool_call"
        end_tag = "<|tool_call|>"
        if end_tag not in text:
            end_tag = "<tool_call|>"
        si = text.find(start_tag)
        ei = text.find(end_tag, si + len(start_tag)) if si >= 0 else -1
        if si >= 0 and ei > si:
            inner = text[si:ei + len(end_tag)]
            call_prefix = "call:"
            cp = inner.find(call_prefix)
            if cp >= 0:
                rest = inner[cp + len(call_prefix):]

                # Find opening delimiter: ( or {
                paren_pos = rest.find("(")
                brace_pos = rest.find("{")
                if paren_pos >= 0 or brace_pos >= 0:
                    if paren_pos >= 0 and (brace_pos < 0 or paren_pos <= brace_pos):
                        close_delim = ")"
                        sep_pos = paren_pos
                    else:
                        close_delim = "}"
                        sep_pos = brace_pos

                    func_name = rest[:sep_pos].strip()
                    if ":" in func_name:
                        func_name = func_name.rsplit(":", 1)[-1]

                    args_raw = rest[sep_pos + 1:]
                    close_tag = close_delim + end_tag
                    ct_pos = args_raw.rfind(close_tag)
                    if ct_pos >= 0:
                        args_raw = args_raw[:ct_pos]
                    else:
                        close_pos = args_raw.rfind(close_delim)
                        if close_pos >= 0:
                            args_raw = args_raw[:close_pos]
                    args_raw = args_raw.strip()

                    # results_ok has no sql argument
                    if func_name == "results_ok":
                        return func_name, ""

                    # Extract sql — try sql= first, then sql:
                    sql_raw = None
                    for sql_prefix in ("sql=", "sql:"):
                        si2 = args_raw.find(sql_prefix)
                        if si2 >= 0:
                            sql_raw = args_raw[si2 + len(sql_prefix):].lstrip()
                            break
                    if sql_raw is not None:
                        # Strip Gemma-escaped quotes: <|"|> → "
                        sql_raw = sql_raw.replace('<|"|>', '"').replace("<|\\\"|>", '"')
                        # Strip wrapping quotes (single or double)
                        if len(sql_raw) >= 2 and sql_raw[0] in ('"', "'") and sql_raw[-1] == sql_raw[0]:
                            sql_raw = sql_raw[1:-1]
                        sql_raw = sql_raw.strip()
                        if sql_raw:
                            return func_name, sql_raw

        # Try Gemma-style <tool_call> format (no pipe prefix):
        #   <tool_call>
        #     <function=run_sql_query>
        #       <parameter=sql>...SQL...</parameter>
        #     </function>
        #   </tool_call>
        gemma_start = "<tool_call>"
        gemma_end = "</tool_call>"
        si = text.find(gemma_start)
        ei = text.find(gemma_end, si + len(gemma_start)) if si >= 0 else -1
        if si >= 0 and ei > si:
            inner = text[si + len(gemma_start):ei].strip()
            func_match = re.search(r'<function\s*=\s*(\w+)\s*>', inner)
            if func_match:
                func_name = func_match.group(1)
                if func_name == "results_ok":
                    return func_name, ""
                if func_name == "run_sql_query":
                    sql_match = re.search(r'<parameter\s*=\s*sql\s*>([\s\S]*?)</parameter>', inner)
                    if sql_match:
                        sql_raw = sql_match.group(1).strip()
                        if sql_raw:
                            return func_name, sql_raw

        return None
    @staticmethod
    def strip_think_tags(text: str) -> str:
        """Strip <think>...</think> blocks from reasoning-model output (DeepSeek-R1, QwQ)."""
        return re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.IGNORECASE).strip()

    @staticmethod
    def strip_markdown_fences(text: Any) -> str:
        raw = str(text or "").strip()
        if not raw:
            return ""
        # Strip reasoning tags before fence extraction
        raw = SqlBenchmarkRunner.strip_think_tags(raw)
        matches = re.findall(r"```(?:\w+)?\s*([\s\S]*?)```", raw)
        if matches:
            raw = matches[0].strip()
        raw = raw.strip().strip("`").strip()
        raw = SqlBenchmarkRunner._convert_mssql_brackets_to_duckdb(raw)
        return raw

    @staticmethod
    def _convert_mssql_brackets_to_duckdb(sql: str) -> str:
        """Convert MSSQL-style ``[identifier]`` to DuckDB ``"identifier"``.

        Only rewrites bracketed identifiers used in identifier position. DuckDB
        list literals (``[1,2,3]``), list indexing (``arr[1]``) and bracket text
        inside single-quoted string literals are left untouched — converting
        them would corrupt otherwise-valid model SQL and cause a false fail.
        """
        out: List[str] = []
        i = 0
        n = len(sql)
        while i < n:
            if sql[i] == "'":
                # Copy a single-quoted string literal verbatim, honoring the
                # SQL '' escape so an embedded quote doesn't end it early.
                j = i + 1
                while j < n:
                    if sql[j] == "'":
                        if j + 1 < n and sql[j + 1] == "'":
                            j += 2
                            continue
                        j += 1
                        break
                    j += 1
                out.append(sql[i:j])
                i = j
            else:
                # Apply the bracket regex only to this non-string chunk.
                j = sql.find("'", i)
                if j == -1:
                    j = n
                out.append(SqlBenchmarkRunner._MSSQL_BRACKET_RE.sub(r'"\1"', sql[i:j]))
                i = j
        return "".join(out)

    @staticmethod
    def normalize_sql(sql: str) -> str:
        cleaned = sqlparse.format(
            sql,
            keyword_case="upper",
            strip_comments=True,
            reindent=False,
        ).strip()
        cleaned = cleaned.rstrip(";").strip()
        return cleaned

    def _load_questions(self) -> List[Dict[str, Any]]:
        if not self.questions_path.exists():
            raise FileNotFoundError(f"Missing questions file: {self.questions_path}")
        payload = json.loads(self.questions_path.read_text(encoding="utf-8"))
        questions = payload.get("questions")
        if not isinstance(questions, list) or not questions:
            raise ValueError(f"Invalid questions payload in {self.questions_path}")
        return [question for question in questions if isinstance(question, dict)]

    def _load_tables_into_duckdb(self) -> Dict[str, List[Dict[str, str]]]:
        if not self.tables_dir.exists():
            raise FileNotFoundError(f"Missing tables directory: {self.tables_dir}")

        schemas: Dict[str, List[Dict[str, str]]] = {}
        csv_paths = sorted(self.tables_dir.glob("*.csv"))
        if not csv_paths:
            raise FileNotFoundError(f"No CSV files found under {self.tables_dir}")

        for csv_path in csv_paths:
            table_name = csv_path.stem
            quoted_table_name = self._quote_identifier(table_name)
            escaped_csv_path = str(csv_path).replace("'", "''")
            self.connection.execute(
                f"CREATE OR REPLACE TABLE {quoted_table_name} AS SELECT * FROM read_csv_auto('{escaped_csv_path}', HEADER=TRUE)"
            )
            schema_rows = self.connection.execute(f"PRAGMA table_info({quoted_table_name})").fetchall()
            schemas[table_name] = [
                {"name": str(row[1]), "type": str(row[2])}
                for row in schema_rows
            ]
        return schemas

    def _schema_block(self, table_name: str) -> str:
        if table_name not in self.table_schemas:
            raise KeyError(f"Unknown table in question schema: {table_name}")
        quoted_table = self._quote_identifier(table_name)
        col_list = ", ".join(
            f"{self._quote_identifier(col['name'])} ({col['type']})"
            for col in self.table_schemas[table_name]
        )
        return f'Table {quoted_table}:\n  Columns: {col_list}'

    def _execute_sql(self, sql: str) -> QueryExecutionResult:
        timeout_s = self.sql_execution_timeout_s
        if not timeout_s or timeout_s <= 0:
            return self._run_sql(sql)

        # DuckDB is blocking, so we can't await a timeout. A watchdog thread
        # fires connection.interrupt() if the query overruns; interrupt() is
        # designed for exactly this cross-thread cancellation and makes the
        # in-flight execute()/fetchall() raise a duckdb.Error.
        timed_out = {"fired": False}

        def _on_timeout() -> None:
            timed_out["fired"] = True
            try:
                self.connection.interrupt()
            except Exception:
                pass

        timer = threading.Timer(timeout_s, _on_timeout)
        timer.start()
        try:
            return self._run_sql(sql)
        except duckdb.Error as exc:
            if timed_out["fired"]:
                raise SqlExecutionTimeout(
                    f"SQL execution exceeded {timeout_s:g}s and was cancelled"
                ) from exc
            raise
        finally:
            # cancel() is a no-op if the timer already fired; the tiny race
            # where it fires just as the query finishes is harmless because the
            # next _execute_sql starts its own fresh watchdog.
            timer.cancel()

    def _run_sql(self, sql: str) -> QueryExecutionResult:
        cursor = self.connection.execute(sql)
        rows = cursor.fetchall()
        description = cursor.description or []
        columns = [str(item[0]) for item in description]
        return QueryExecutionResult(columns=columns, rows=rows)

    def build_skipped_result(
        self,
        *,
        question_id: int,
        model: str,
        provider: str,
        endpoint: str,
        thinking_mode: str,
        reason: str,
    ) -> Dict[str, Any]:
        """Result for a question deliberately NOT run because the model is
        unavailable (load/connection failure or absent weights).

        No SQL is executed; expected_* are read straight from the question so
        the row shape matches a real failure. ``stop_reason`` is
        ``"skipped_model_unavailable"`` so the UI/aggregates can tell a skip
        apart from a genuine attempt that errored.
        """
        question = self.questions_by_id.get(int(question_id)) or {"id": int(question_id)}
        first_row = question.get("first_row")
        return self._build_failure_result(
            question=question,
            model=model,
            provider=provider,
            endpoint=endpoint,
            expected_sql=str(question.get("sql", "")),
            expected_row_count=_safe_optional_int(question.get("row_count")),
            expected_columns=[str(column) for column in question.get("columns") or []],
            expected_first_row=_normalize_mapping(first_row) if isinstance(first_row, dict) else None,
            generated_sql="",
            error=reason,
            conversation=[],
            thinking_mode=thinking_mode,
            stop_reason="skipped_model_unavailable",
        )

    def _build_failure_result(
        self,
        *,
        question: Dict[str, Any],
        model: str,
        provider: str,
        endpoint: str,
        expected_sql: str,
        expected_row_count: Optional[int],
        expected_columns: Optional[List[str]],
        expected_first_row: Optional[Dict[str, Any]],
        generated_sql: str,
        error: str,
        conversation: Optional[List[Dict[str, Any]]] = None,
        thinking_mode: str = "off",
        stop_reason: str = "error",
    ) -> Dict[str, Any]:
        return {
            "benchmark_type": "sql",
            "question_id": int(question["id"]),
            "difficulty": str(question.get("difficulty", "")),
            "question": str(question.get("question", "")),
            "model": model,
            "thinking_mode": thinking_mode,
            "provider": provider,
            "endpoint": endpoint,
            "generated_sql": generated_sql,
            "expected_sql": expected_sql,
            "success": False,
            "outcome": "error",
            "error": error,
            "stop_reason": stop_reason,
            "expected_row_count": expected_row_count,
            "actual_row_count": None,
            "expected_columns": expected_columns,
            "actual_columns": None,
            "expected_first_row": expected_first_row,
            "actual_first_row": None,
            "row_count_match": None,
            "columns_match": None,
            "first_row_match": None,
            "first_row_diffs": [],
            "conversation": conversation if conversation is not None else [],
        }

    @staticmethod
    def _build_mismatch_error(*, row_count_match: bool, columns_match: bool, first_row_match: bool) -> str:
        failed_checks = []
        if not row_count_match:
            failed_checks.append("row_count")
        if not columns_match:
            failed_checks.append("columns")
        if not first_row_match:
            failed_checks.append("first_row")
        return f"Result mismatch: {', '.join(failed_checks)}"

    @staticmethod
    def _quote_identifier(name: str) -> str:
        return '"' + str(name).replace('"', '""') + '"'


def _compute_first_row_diffs(
    expected: Dict[str, Any],
    actual: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Compute detailed first_row diffs (from original check.ts firstRowDiffs).
    Returns list of {column, expected, actual} for mismatched columns.
    """
    diffs = []
    for col in expected:
        if col not in actual:
            continue
        ev = expected[col]
        av = actual[col]
        if _is_number(ev) and _is_number(av):
            tolerance = _rounding_tolerance(ev)
            epsilon = 2.220446049250313e-16 * max(abs(float(av)), abs(float(ev)))
            if abs(float(av) - float(ev)) <= tolerance + epsilon:
                continue
        elif str(av) == str(ev):
            continue
        diffs.append({"column": col, "expected": ev, "actual": av})
    return diffs



def _normalize_mapping(value: Any) -> Optional[Dict[str, Any]]:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"Expected dict or None, got: {type(value)!r}")
    return {str(key): _to_json_compatible(item) for key, item in value.items()}



def _safe_int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(fallback)



def _safe_optional_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None



def _to_json_compatible(value: Any) -> Any:
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_to_json_compatible(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _to_json_compatible(item) for key, item in value.items()}
    return value



def _normalize_col(name: str) -> str:
    """Normalize column name for case/insensitive comparison (from original check.ts).
    "Fiscal Year" = "fiscal_year" = "FiscalYear" → "fiscalyear"
    """
    return re.sub(r'[^a-z0-9]', '', name.lower())


def _columns_match(expected: List[str], actual: List[str]) -> bool:
    """Compare column lists with normalization (from original check.ts normalizeCol)."""
    if len(expected) != len(actual):
        return False
    exp_norm = sorted(_normalize_col(c) for c in expected)
    act_norm = sorted(_normalize_col(c) for c in actual)
    return exp_norm == act_norm

def _values_match(expected: Any, actual: Any) -> bool:
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return False
        # Normalize keys for comparison (original uses normalizeCol)
        expected_norm = {_normalize_col(k): (k, v) for k, v in expected.items()}
        actual_norm = {_normalize_col(k): (k, v) for k, v in actual.items()}
        if set(expected_norm.keys()) != set(actual_norm.keys()):
            return False
        return all(
            _values_match(expected_norm[key][1], actual_norm[key][1])
            for key in expected_norm
        )

    if isinstance(expected, list):
        if not isinstance(actual, list) or len(expected) != len(actual):
            return False
        return all(_values_match(exp, act) for exp, act in zip(expected, actual))

    if _is_number(expected) and _is_number(actual):
        # Adaptive tolerance from original check.ts: half a unit in the last
        # decimal place of expected, so a model's unrounded value still matches
        # the rounded reference.
        tolerance = _rounding_tolerance(expected)
        epsilon = 2.220446049250313e-16 * max(abs(float(actual)), abs(float(expected)))
        return abs(float(actual) - float(expected)) <= tolerance + epsilon

    return expected == actual



def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)


def _rounding_tolerance(expected: Any) -> float:
    """Half a unit in the last decimal place of *expected*.

    Absorbs exactly one rounding step at the precision the reference value was
    rounded to (e.g. 12.7 -> 0.05, integer 100 -> 0.5). The decimal count comes
    from ``Decimal`` so scientific-notation floats (``1e-05`` -> 5 decimals,
    ``1e20`` -> 0) get the right exponent instead of the old ``str()`` scan,
    which saw no ``.`` and fell back to a meaningless whole-unit tolerance.
    """
    try:
        exponent = Decimal(str(expected)).as_tuple().exponent
    except (InvalidOperation, ValueError):
        exponent = 0
    decimals = -exponent if isinstance(exponent, int) and exponent < 0 else 0
    return 5 * (10 ** -(decimals + 1))
