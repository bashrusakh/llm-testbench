import asyncio
from pathlib import Path

import pytest

from python.sql_benchmark import SqlBenchmarkRunner


DATA_DIR = Path(__file__).resolve().parents[1] / 'sql_benchmark_data'


def run(coro):
    return asyncio.run(coro)


# ── strip_markdown_fences ─────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("```sql\nSELECT 1\n```", "SELECT 1"),
    ("```\nSELECT 1;```", "SELECT 1;"),
    ("`SELECT 1`", "SELECT 1"),
    ("SELECT 1", "SELECT 1"),
    ("", ""),
    (None, ""),
    ("```sql\n\n```", ""),
    ("```python\nprint('hello')\n```", "print('hello')"),
    (123, "123"),
])
def test_strip_markdown_fences(raw, expected):
    assert SqlBenchmarkRunner.strip_markdown_fences(raw) == expected


# ── _looks_like_sql ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("SELECT 1", True),
    ("WITH cte AS (SELECT 1) SELECT * FROM cte", True),
    ("select * from foo", True),
    ("with x as (select 1) select * from x", True),
    ("not sql text", False),
    ("", False),
    # Leading -- comments must be skipped
    ("-- comment\nSELECT 1", True),
    ("-- comment\n\nSELECT 1", True),
    ("-- comment\nWITH cte AS (SELECT 1)\nSELECT * FROM cte", True),
    ("  -- indented comment\n  SELECT 1", True),
    ("-- multiple\n-- comments\n-- before\nSELECT 1", True),
    ("-- only comments\n-- no sql here", False),
    # Thinking text with ellipsis must NOT be recognised as SQL
    ("SELECT p.\"Product\",\nFROM Product AS P\n\nJOIN Sales S ON ... etc.", False),
    ("SELECT\n    p.\"Product\",\n    ...\nFROM Product", False),
    ("SELECT ... FROM Product", False),
])
def test_looks_like_sql(raw, expected):
    assert SqlBenchmarkRunner._looks_like_sql(raw) == expected


# ── _convert_mssql_brackets_to_duckdb ─────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("SUM(s.[Sales Amount])", 'SUM(s."Sales Amount")'),
    ("[user_id]", '"user_id"'),
    ("a.[col 1], b.[col 2]", 'a."col 1", b."col 2"'),
    ("SELECT [Revenue] FROM [Sales]", 'SELECT "Revenue" FROM "Sales"'),
    ("no brackets here", "no brackets here"),
    ("", ""),
    # Hazards that must NOT be rewritten (valid DuckDB / string content):
    ("SELECT [1,2,3] AS arr", "SELECT [1,2,3] AS arr"),   # list literal
    ("SELECT arr[1] FROM t", "SELECT arr[1] FROM t"),     # list indexing
    ("SELECT arr[1:2] FROM t", "SELECT arr[1:2] FROM t"), # list slice
    ("WHERE path = '$[0]'", "WHERE path = '$[0]'"),       # bracket in string
    ("WHERE name = '[Region]'", "WHERE name = '[Region]'"),  # ident-like in string
])
def test_convert_mssql_brackets_to_duckdb(raw, expected):
    assert SqlBenchmarkRunner._convert_mssql_brackets_to_duckdb(raw) == expected


# ── combined fence stripping + bracket conversion ─────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    (
        "```sql\nSELECT SUM(s.[Sales Amount]) FROM Sales s\n```",
        'SELECT SUM(s."Sales Amount") FROM Sales s',
    ),
    (
        "```\nSELECT [user_id] FROM t\n```",
        'SELECT "user_id" FROM t',
    ),
])
def test_strip_and_bracket_convert(raw, expected):
    result = SqlBenchmarkRunner.strip_markdown_fences(raw)
    assert result == expected


# ── numeric tolerance (_rounding_tolerance / _values_match) ───────────────────

from python.sql_benchmark import _rounding_tolerance, _values_match  # noqa: E402


@pytest.mark.parametrize("expected,tol", [
    (12.7, 0.05),       # 1 decimal -> half of 0.1
    (73.3, 0.05),
    (3.14, 0.005),      # 2 decimals
    (100, 0.5),         # integer -> half unit
    (10918, 0.5),
])
def test_rounding_tolerance_normal(expected, tol):
    assert _rounding_tolerance(expected) == pytest.approx(tol)


def test_rounding_tolerance_handles_scientific_notation():
    # str(1e-05) == '1e-05' has no '.', so the old digit-after-dot scan gave 0
    # decimals -> tolerance 0.5, falsely matching wildly different values.
    assert _rounding_tolerance(1e-05) == pytest.approx(5e-06)
    assert _rounding_tolerance(1e20) == pytest.approx(0.5)


@pytest.mark.parametrize("expected,actual,match", [
    (1e-05, 0.4, False),    # was a false PASS before the fix
    (1e-05, 1.0e-05, True),
    (12.7, 12.72, True),    # model's unrounded value vs rounded reference
    (12.7, 12.6, False),
    (100, 100.4, True),     # int half-unit tolerance
    (100, 101, False),
])
def test_values_match_numeric_tolerance(expected, actual, match):
    assert _values_match(expected, actual) is match


# ── normalize_sql ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("select 1", "SELECT 1"),
    ("SELECT 1;", "SELECT 1"),
    ("  select   42  ", "SELECT   42"),
])
def test_normalize_sql_uppercases_keywords(raw, expected):
    assert SqlBenchmarkRunner.normalize_sql(raw) == expected


def test_normalize_sql_strips_trailing_semicolon():
    assert SqlBenchmarkRunner.normalize_sql("SELECT 1;") == "SELECT 1"


def test_normalize_sql_strips_trailing_semicolons():
    assert SqlBenchmarkRunner.normalize_sql("SELECT 1;;;") == "SELECT 1"


# ── integration: full cleanup pipeline ───────────────────────────────────────

def test_full_cleanup_pipeline():
    raw = "```sql\nSELECT SUM(s.[Sales Amount]), t.[Fiscal Year]\nFROM Sales s\nJOIN Date t ON s.OrderDateKey = t.DateKey\nGROUP BY t.[Fiscal Year];\n```"
    after_strip = SqlBenchmarkRunner.strip_markdown_fences(raw)
    after_normalize = SqlBenchmarkRunner.normalize_sql(after_strip)
    assert "```" not in after_normalize
    assert "[" not in after_normalize
    assert "]" not in after_normalize
    assert "GROUP BY" in after_normalize


def test_runner_accepts_fenced_sql_with_brackets():
    captured = {}
    runner = None

    async def llm_callback(system, user, *, model, provider, endpoint, timeout_ms):
        captured['prompt'] = user
        expected_sql = runner.questions_by_id[1]['sql']
        # wrap in fenced markdown with MS-SQL bracket syntax — runner must clean both
        bracket_sql = expected_sql.replace('"Sales Amount"', '[Sales Amount]').replace('"Fiscal Year"', '[Fiscal Year]').replace('"Total Product Cost"', '[Total Product Cost]')
        return f"```sql\n{bracket_sql}\n```"

    with SqlBenchmarkRunner(llm_callback=llm_callback, data_dir=DATA_DIR) as created_runner:
        runner = created_runner
        result = run(
            runner.run_question(
                question_id=1,
                model='stub-model',
                provider='openai-compatible',
                endpoint='http://127.0.0.1:1234',
                timeout_ms=120000,
            )
        )

    assert captured['prompt']
    assert result['benchmark_type'] == 'sql'
    assert result['question_id'] == 1
    assert result['success'] is True
    assert result['row_count_match'] is True
    assert result['columns_match'] is True
    assert result['first_row_match'] is True
    assert result['generated_sql'].startswith('SELECT')
    assert result['error'] == ''
    # verify brackets were converted to double quotes
    assert '[' not in result['generated_sql']
    assert ']' not in result['generated_sql']


def test_sql_runner_fails_when_generated_sql_is_empty_after_cleanup():
    async def llm_callback(system, user, *, model, provider, endpoint, timeout_ms):
        return '```sql\n```'

    with SqlBenchmarkRunner(llm_callback=llm_callback, data_dir=DATA_DIR) as runner:
        result = run(
            runner.run_question(
                question_id=1,
                model='stub-model',
                provider='openai-compatible',
                endpoint='http://127.0.0.1:1234',
                timeout_ms=120000,
            )
        )

    assert result['success'] is False
    assert 'empty' in result['error'].lower()
    assert result['generated_sql'] == ''
    assert result['actual_row_count'] is None
    assert result['row_count_match'] is None


def test_prompt_uses_only_included_table_schema_subset():
    captured = {}
    runner = None

    async def llm_callback(system, user, *, model, provider, endpoint, timeout_ms):
        captured['system'] = system
        captured['user'] = user
        return runner.questions_by_id[1]['sql']

    with SqlBenchmarkRunner(llm_callback=llm_callback, data_dir=DATA_DIR) as created_runner:
        runner = created_runner
        run(
            runner.run_question(
                question_id=1,
                model='stub-model',
                provider='openai-compatible',
                endpoint='http://127.0.0.1:1234',
                timeout_ms=120000,
            )
        )

    # Schema is now in system prompt, not user
    system = captured['system']
    assert 'Table "Sales":' in system
    assert 'Table "Date":' in system
    assert 'Table "Product"' not in system
    assert 'Table "Customer"' not in system
    # User prompt contains only the question
    user = captured['user']
    assert 'Show annual revenue' in user


def test_run_all_respects_empty_question_id_list():
    async def llm_callback(prompt, *, model, provider, endpoint, timeout_ms):
        raise AssertionError('callback should not be called for empty question list')

    with SqlBenchmarkRunner(llm_callback=llm_callback, data_dir=DATA_DIR) as runner:
        results = run(
            runner.run_all(
                question_ids=[],
                model='stub-model',
                provider='openai-compatible',
                endpoint='http://127.0.0.1:1234',
                timeout_ms=120000,
            )
        )

    assert results == []


# ── outcome field ─────────────────────────────────────────────────────────────

def _make_tool_response(func_name: str, arguments: dict, tc_id: str = "tc1") -> dict:
    """Helper: build a minimal tool-call LLM response."""
    import json as _json
    return {
        "content": "",
        "tool_calls": [{
            "id": tc_id,
            "type": "function",
            "function": {"name": func_name, "arguments": _json.dumps(arguments)},
        }],
        "usage": {},
        "model": "stub-model",
    }


def test_tool_calling_outcome_pass_on_correct_answer():
    """outcome='pass' when results_ok is called and validation succeeds."""
    runner = None
    call_count = [0]

    async def tool_callback(*, system_prompt, messages, tools, model, provider, endpoint, timeout_ms):
        call_count[0] += 1
        if call_count[0] == 1:
            expected_sql = runner.questions_by_id[1]['sql']
            return _make_tool_response("run_sql_query", {"sql": expected_sql})
        return _make_tool_response("results_ok", {})

    with SqlBenchmarkRunner(llm_callback=None, data_dir=DATA_DIR) as created_runner:
        runner = created_runner
        result = run(
            runner.run_question_tool_calling(
                question_id=1,
                model='stub-model',
                provider='openai-compatible',
                endpoint='http://127.0.0.1:1234',
                timeout_ms=120000,
                tool_llm_callback=tool_callback,
            )
        )

    assert result['outcome'] == 'pass'
    assert result['success'] is True


def test_tool_calling_outcome_fail_on_wrong_answer():
    """outcome='fail' when results_ok is called but validation fails."""
    runner = None
    call_count = [0]

    async def tool_callback(*, system_prompt, messages, tools, model, provider, endpoint, timeout_ms):
        call_count[0] += 1
        if call_count[0] == 1:
            return _make_tool_response("run_sql_query", {"sql": "SELECT 0 AS wrong_col"})
        return _make_tool_response("results_ok", {})

    with SqlBenchmarkRunner(llm_callback=None, data_dir=DATA_DIR) as created_runner:
        runner = created_runner
        result = run(
            runner.run_question_tool_calling(
                question_id=1,
                model='stub-model',
                provider='openai-compatible',
                endpoint='http://127.0.0.1:1234',
                timeout_ms=120000,
                tool_llm_callback=tool_callback,
            )
        )

    assert result['outcome'] == 'fail'
    assert result['success'] is False


def test_tool_calling_outcome_error_on_llm_failure():
    """outcome='error' when LLM callback raises."""
    runner = None

    async def tool_callback(*, system_prompt, messages, tools, model, provider, endpoint, timeout_ms):
        raise RuntimeError("LLM unavailable")

    with SqlBenchmarkRunner(llm_callback=None, data_dir=DATA_DIR) as created_runner:
        runner = created_runner
        result = run(
            runner.run_question_tool_calling(
                question_id=1,
                model='stub-model',
                provider='openai-compatible',
                endpoint='http://127.0.0.1:1234',
                timeout_ms=120000,
                tool_llm_callback=tool_callback,
            )
        )

    assert result['outcome'] == 'error'
    assert result['success'] is False
    assert 'LLM unavailable' in result['error']


# ── conversation persistence ──────────────────────────────────────────────────

def test_tool_calling_saves_conversation_on_pass():
    """conversation is saved and contains user + assistant + tool messages."""
    runner = None
    call_count = [0]

    async def tool_callback(*, system_prompt, messages, tools, model, provider, endpoint, timeout_ms):
        call_count[0] += 1
        if call_count[0] == 1:
            expected_sql = runner.questions_by_id[1]['sql']
            return _make_tool_response("run_sql_query", {"sql": expected_sql})
        return _make_tool_response("results_ok", {})

    with SqlBenchmarkRunner(llm_callback=None, data_dir=DATA_DIR) as created_runner:
        runner = created_runner
        result = run(
            runner.run_question_tool_calling(
                question_id=1,
                model='stub-model',
                provider='openai-compatible',
                endpoint='http://127.0.0.1:1234',
                timeout_ms=120000,
                tool_llm_callback=tool_callback,
            )
        )

    conv = result['conversation']
    assert isinstance(conv, list)
    assert len(conv) >= 1
    roles = [m['role'] for m in conv]
    assert 'user' in roles
    assert 'assistant' in roles or 'tool' in roles


def test_tool_calling_saves_conversation_on_error():
    """conversation is saved even when LLM fails."""
    async def tool_callback(*, system_prompt, messages, tools, model, provider, endpoint, timeout_ms):
        raise RuntimeError("boom")

    with SqlBenchmarkRunner(llm_callback=None, data_dir=DATA_DIR) as runner:
        result = run(
            runner.run_question_tool_calling(
                question_id=1,
                model='stub-model',
                provider='openai-compatible',
                endpoint='http://127.0.0.1:1234',
                timeout_ms=120000,
                tool_llm_callback=tool_callback,
            )
        )

    assert isinstance(result['conversation'], list)


# ── loop: SQL error feedback ──────────────────────────────────────────────────

def test_tool_calling_loop_sends_sql_error_back_to_llm():
    """When SQL fails, the error is fed back to LLM as a tool message, and LLM can retry."""
    runner = None
    received_messages = []
    call_count = [0]

    async def tool_callback(*, system_prompt, messages, tools, model, provider, endpoint, timeout_ms):
        call_count[0] += 1
        received_messages.append(list(messages))
        if call_count[0] == 1:
            # Return intentionally broken SQL
            return _make_tool_response("run_sql_query", {"sql": "SELECT * FROM nonexistent_table_xyz"})
        if call_count[0] == 2:
            # Second call: return correct SQL
            return _make_tool_response("run_sql_query", {"sql": runner.questions_by_id[1]['sql']})
        return _make_tool_response("results_ok", {})

    with SqlBenchmarkRunner(llm_callback=None, data_dir=DATA_DIR) as created_runner:
        runner = created_runner
        result = run(
            runner.run_question_tool_calling(
                question_id=1,
                model='stub-model',
                provider='openai-compatible',
                endpoint='http://127.0.0.1:1234',
                timeout_ms=120000,
                tool_llm_callback=tool_callback,
            )
        )

    # Second call must have received the SQL error as a tool message
    assert call_count[0] >= 2
    second_call_messages = received_messages[1]
    tool_messages = [m for m in second_call_messages if m.get('role') == 'tool']
    assert tool_messages, "LLM must receive a tool message with SQL error feedback"
    error_content = tool_messages[0]['content']
    assert 'error' in error_content.lower() or 'Error' in error_content


# ── no-tool-call path: LLM responds with text ────────────────────────────────

def test_tool_calling_no_tool_call_prompts_llm_to_use_tool_off():
    """When LLM returns text without a tool call (thinking_mode=off):
    - First MAX_NO_TOOL_CALL_RETRIES (3) calls are silent retries (same messages).
    - After retries exhausted, a follow-up message is added asking to use run_sql_query.
    """
    runner = None
    received_messages = []
    call_count = [0]
    MAX_RETRIES = 3  # MAX_NO_TOOL_CALL_RETRIES for thinking_mode=off

    async def tool_callback(*, system_prompt, messages, tools, model, provider, endpoint, timeout_ms):
        call_count[0] += 1
        received_messages.append(list(messages))
        if call_count[0] <= MAX_RETRIES + 1:
            # First attempt + MAX_RETRIES retries all return no tool call
            return {"content": "I think the answer is 42", "tool_calls": [], "usage": {}, "model": "stub-model"}
        # After retries exhausted follow-up was added: now return SQL
        return _make_tool_response("run_sql_query", {"sql": runner.questions_by_id[1]['sql']})

    with SqlBenchmarkRunner(llm_callback=None, data_dir=DATA_DIR) as created_runner:
        runner = created_runner
        result = run(
            runner.run_question_tool_calling(
                question_id=1,
                model='stub-model',
                provider='openai-compatible',
                endpoint='http://127.0.0.1:1234',
                timeout_ms=120000,
                tool_llm_callback=tool_callback,
                thinking_mode="off",
            )
        )

    # After MAX_RETRIES silent retries, a follow-up call is made
    assert call_count[0] > MAX_RETRIES + 1, f"Expected >{MAX_RETRIES + 1} calls, got {call_count[0]}"
    # The call after retries exhausted must have a message containing run_sql_query
    followup_call_messages = received_messages[MAX_RETRIES + 1]
    contents = " ".join(
        m.get("content", "") or "" for m in followup_call_messages if isinstance(m.get("content"), str)
    )
    assert "run_sql_query" in contents


def test_tool_calling_no_tool_call_retries_less_for_thinking_on():
    """With thinking_mode=on, MAX_NO_TOOL_CALL_RETRIES is 1 (not 3), so
    after 1 silent retry we nudge immediately: total calls = 4
    (1 silent + 1 nudge + 1 tool_call + 1 results_ok)."""
    runner = None
    call_count = [0]

    async def tool_callback(*, system_prompt, messages, tools, model, provider, endpoint, timeout_ms):
        call_count[0] += 1
        if call_count[0] <= 2:
            # Call 1: no tool call → silent retry
            # Call 2: no tool call → nudge
            return {"content": "<think>Let me reason...</think>", "tool_calls": [], "usage": {}, "model": "stub-model"}
        if call_count[0] == 3:
            # Call 3: after nudge, returns correct SQL
            return _make_tool_response("run_sql_query", {"sql": runner.questions_by_id[1]['sql']})
        # Call 4: results_ok to finish
        return _make_tool_response("results_ok", {})

    with SqlBenchmarkRunner(llm_callback=None, data_dir=DATA_DIR) as created_runner:
        runner = created_runner
        result = run(
            runner.run_question_tool_calling(
                question_id=1,
                model='stub-model',
                provider='openai-compatible',
                endpoint='http://127.0.0.1:1234',
                timeout_ms=120000,
                tool_llm_callback=tool_callback,
                thinking_mode="on",
            )
        )

    assert call_count[0] == 4, f"Expected 4 calls (1 silent + 1 nudge + 1 tool + 1 ok), got {call_count[0]}"
    assert result['success'] is True


def test_tool_calling_extracts_sql_from_text_with_leading_comment():
    """Model returns text with SQL in ``` fences that has a leading -- comment.
    _looks_like_sql must skip the comment and recognize the SELECT, not fall
    into silent retries."""
    runner = None
    call_count = [0]

    async def tool_callback(*, system_prompt, messages, tools, model, provider, endpoint, timeout_ms):
        call_count[0] += 1
        if call_count[0] == 1:
            # First call: return prose with --comment SQL inside ```sql fences
            expected_sql = runner.questions_by_id[1]['sql']
            return {
                "content": f"""Here is the query:

```sql
-- This query answers the question
{expected_sql}
```""",
                "tool_calls": [],
                "usage": {},
                "model": "stub-model",
            }
        return _make_tool_response("results_ok", {})

    with SqlBenchmarkRunner(llm_callback=None, data_dir=DATA_DIR) as created_runner:
        runner = created_runner
        result = run(
            runner.run_question_tool_calling(
                question_id=1,
                model='stub-model',
                provider='openai-compatible',
                endpoint='http://127.0.0.1:1234',
                timeout_ms=120000,
                tool_llm_callback=tool_callback,
            )
        )

    assert call_count[0] < 3, f"Expected 1 or 2 calls (SQL extracted from text), got {call_count[0]}"
    assert result['success'] is True
    assert result['stop_reason'] == 'results_ok'


def test_tool_calling_text_fallback_retry_on_sql_error():
    """When text-only response is extracted as SQL but fails DuckDB execution,
    the error is fed back and the model gets a chance to retry with a tool call.
    Mirrors the normal tool-calling retry path (lines 794-815 of sql_benchmark.py)."""
    runner = None
    received_messages = []
    call_count = [0]

    async def tool_callback(*, system_prompt, messages, tools, model, provider, endpoint, timeout_ms):
        call_count[0] += 1
        received_messages.append(list(messages))
        if call_count[0] == 1:
            # First call: return text-only (no tool_calls) with SQL that will
            # be extracted by strip_markdown_fences + _looks_like_sql, but
            # fail DuckDB because the table doesn't exist.
            return {
                "content": "SELECT * FROM nonexistent_table_xyz",
                "tool_calls": [],
                "usage": {},
                "model": "stub-model",
            }
        if call_count[0] == 2:
            # Second call: after receiving error feedback, model uses the proper
            # run_sql_query tool call with correct SQL.
            return _make_tool_response("run_sql_query", {"sql": runner.questions_by_id[1]['sql']})
        return _make_tool_response("results_ok", {})

    with SqlBenchmarkRunner(llm_callback=None, data_dir=DATA_DIR) as created_runner:
        runner = created_runner
        result = run(
            runner.run_question_tool_calling(
                question_id=1,
                model='stub-model',
                provider='openai-compatible',
                endpoint='http://127.0.0.1:1234',
                timeout_ms=120000,
                tool_llm_callback=tool_callback,
            )
        )

    assert call_count[0] >= 2, f"Expected >=2 calls (retry after text-fallback error), got {call_count[0]}"
    # Second call must have received the error feedback as a tool message
    second_call_messages = received_messages[1]
    tool_messages = [m for m in second_call_messages if m.get('role') == 'tool']
    assert tool_messages, "LLM must receive a tool message with SQL error after text-fallback"
    error_content = tool_messages[0]['content']
    assert 'error' in error_content.lower() or 'Error' in error_content
    assert result['success'] is True


# ── _parse_custom_tool_call ──────────────────────────────────────────────────

def test_parse_custom_tool_call_gemma_run_sql():
    """Gemma-style <tool_call> with run_sql_query is parsed correctly."""
    text = """Let me write the query.

<tool_call>
<function=run_sql_query>
<parameter=sql>
SELECT * FROM "Sales" LIMIT 5
</parameter>
</function>
</tool_call>"""
    result = SqlBenchmarkRunner._parse_custom_tool_call(text)
    assert result is not None
    func, sql = result
    assert func == "run_sql_query"
    assert "SELECT * FROM" in sql
    assert '"Sales"' in sql


def test_parse_custom_tool_call_gemma_results_ok():
    """Gemma-style <tool_call> with results_ok is parsed correctly."""
    text = "<tool_call>\n<function=results_ok>\n</function>\n</tool_call>"
    result = SqlBenchmarkRunner._parse_custom_tool_call(text)
    assert result == ("results_ok", "")


def test_parse_custom_tool_call_gemma_missing_sql_param():
    """Gemma block missing <parameter=sql> returns None (no SQL to extract)."""
    text = "<tool_call>\n<function=run_sql_query>\n</function>\n</tool_call>"
    result = SqlBenchmarkRunner._parse_custom_tool_call(text)
    assert result is None


def test_parse_custom_tool_call_gemma_missing_function_close():
    """Gemma block missing </function> still parses if <parameter=sql> is present."""
    text = "<tool_call>\n<function=run_sql_query>\n<parameter=sql>SELECT 1</parameter>\n</tool_call>"
    result = SqlBenchmarkRunner._parse_custom_tool_call(text)
    assert result is not None
    assert result[0] == "run_sql_query"


def test_parse_custom_tool_call_gemma_whitespace_around_equals():
    """Gemma block with spaces around = is still parsed."""
    text = "<tool_call>\n<function = run_sql_query>\n<parameter = sql>\nSELECT 1\n</parameter>\n</function>\n</tool_call>"
    result = SqlBenchmarkRunner._parse_custom_tool_call(text)
    assert result is not None
    assert result[0] == "run_sql_query"


def test_parse_custom_tool_call_pipe_format():
    """Pipe-prefixed format still works (regression)."""
    text = '<|tool_call|>call:run_sql_query(sql="SELECT 1")<|tool_call|>'
    result = SqlBenchmarkRunner._parse_custom_tool_call(text)
    assert result is not None
    assert result[0] == "run_sql_query"
    assert "SELECT 1" in result[1]


def test_parse_custom_tool_call_gemma_first_block_only():
    """Multiple Gemma blocks parse only the first, not spanning all."""
    text = """<tool_call>
<function=run_sql_query>
<parameter=sql>
SELECT 1
</parameter>
</function>
</tool_call>
some text
<tool_call>
<function=run_sql_query>
<parameter=sql>
SELECT 2
</parameter>
</function>
</tool_call>"""
    result = SqlBenchmarkRunner._parse_custom_tool_call(text)
    assert result is not None
    func, sql = result
    assert func == "run_sql_query"
    assert "SELECT 1" in sql
    assert "SELECT 2" not in sql


def test_parse_custom_tool_call_no_match():
    """Plain text with no tool-call markers returns None."""
    result = SqlBenchmarkRunner._parse_custom_tool_call("I think the answer is 42")
    assert result is None


# ── grammar mode: run_question has outcome + conversation ────────────────────

def test_run_question_grammar_mode_outcome_pass():
    """run_question (grammar mode) returns outcome='pass' on success."""
    runner = None

    async def llm_callback(system, user, *, model, provider, endpoint, timeout_ms):
        return runner.questions_by_id[1]['sql']

    with SqlBenchmarkRunner(llm_callback=llm_callback, data_dir=DATA_DIR) as created_runner:
        runner = created_runner
        result = run(
            runner.run_question(
                question_id=1,
                model='stub-model',
                provider='openai-compatible',
                endpoint='http://127.0.0.1:1234',
                timeout_ms=120000,
            )
        )

    assert result['outcome'] == 'pass'
    assert result['success'] is True
    assert result['conversation'] == []


def test_run_question_grammar_mode_outcome_fail_on_wrong_sql():
    """run_question returns outcome='fail' when SQL runs but results don't match."""
    async def llm_callback(system, user, *, model, provider, endpoint, timeout_ms):
        return "SELECT 0 AS wrong"

    with SqlBenchmarkRunner(llm_callback=llm_callback, data_dir=DATA_DIR) as runner:
        result = run(
            runner.run_question(
                question_id=1,
                model='stub-model',
                provider='openai-compatible',
                endpoint='http://127.0.0.1:1234',
                timeout_ms=120000,
            )
        )

    assert result['outcome'] == 'fail'
    assert result['success'] is False


def test_run_question_grammar_mode_outcome_error_on_llm_failure():
    """run_question returns outcome='error' when LLM raises."""
    async def llm_callback(system, user, *, model, provider, endpoint, timeout_ms):
        raise RuntimeError("LLM down")

    with SqlBenchmarkRunner(llm_callback=llm_callback, data_dir=DATA_DIR) as runner:
        result = run(
            runner.run_question(
                question_id=1,
                model='stub-model',
                provider='openai-compatible',
                endpoint='http://127.0.0.1:1234',
                timeout_ms=120000,
            )
        )

    assert result['outcome'] == 'error'
    assert result['success'] is False


# ── SQL execution timeout (watchdog) ─────────────────────────────────────────

import time as _time  # noqa: E402

import duckdb as _duckdb  # noqa: E402

from python.sql_benchmark import SqlExecutionTimeout  # noqa: E402


# Runaway cross join: ~10^10 rows with a per-row predicate so DuckDB cannot
# shortcut it analytically. Slow enough to overrun a sub-second budget, and
# connection.interrupt() cancels it promptly.
_RUNAWAY_SQL = (
    "SELECT count(*) FROM range(100000) a(x), range(100000) b(y) "
    "WHERE a.x * b.y >= 0"
)


def test_execute_sql_times_out_on_runaway_query():
    """A query exceeding sql_execution_timeout_s is interrupted and raises
    SqlExecutionTimeout (a duckdb.Error); the connection stays usable."""
    with SqlBenchmarkRunner(llm_callback=None, data_dir=DATA_DIR,
                            sql_execution_timeout_s=0.3) as runner:
        start = _time.perf_counter()
        with pytest.raises(SqlExecutionTimeout):
            runner._execute_sql(_RUNAWAY_SQL)
        elapsed = _time.perf_counter() - start
        # Cancelled near the budget, not run to completion.
        assert elapsed < 10.0
        # Subclasses duckdb.Error so existing call-site handlers catch it.
        assert issubclass(SqlExecutionTimeout, _duckdb.Error)
        # Connection survives the interrupt and serves the next query.
        assert runner._execute_sql("SELECT 1").rows == [(1,)]


def test_execute_sql_disabled_timeout_runs_without_watchdog():
    """timeout <= 0 disables the watchdog; normal queries still work."""
    with SqlBenchmarkRunner(llm_callback=None, data_dir=DATA_DIR,
                            sql_execution_timeout_s=0) as runner:
        result = runner._execute_sql("SELECT 1 AS a")
        assert result.columns == ["a"]
        assert result.rows == [(1,)]


def test_run_question_reports_error_on_runaway_generated_sql():
    """A model that emits a runaway query yields outcome='error': the timeout
    flows through the normal duckdb.Error failure path in run_question."""
    async def llm_callback(system, user, *, model, provider, endpoint, timeout_ms):
        return _RUNAWAY_SQL

    with SqlBenchmarkRunner(llm_callback=llm_callback, data_dir=DATA_DIR,
                            sql_execution_timeout_s=0.3) as runner:
        result = run(
            runner.run_question(
                question_id=1,
                model='stub-model',
                provider='openai-compatible',
                endpoint='http://127.0.0.1:1234',
                timeout_ms=120000,
            )
        )

    assert result['outcome'] == 'error'
    assert result['success'] is False
    assert 'exceeded' in result['error'].lower()
