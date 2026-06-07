from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = PROJECT_ROOT / "index.html"
APP_JS = PROJECT_ROOT / "static" / "app.js"


def _read_frontend() -> str:
    """Concatenated index.html + static/app.js for substring assertions.

    Pre-PR-D the JS lived in an inline <script>, so tests searched INDEX_HTML
    for JS-only strings. The split moved the script to static/app.js; this
    helper keeps the existing call sites working without per-test rewrites.
    """
    return INDEX_HTML.read_text("utf-8") + "\n" + APP_JS.read_text("utf-8")


def test_sql_mode_controls_exist_in_frontend():
    html = INDEX_HTML.read_text("utf-8")

    # Test selection and thinking mode are now checkboxes (multi-select).
    assert 'id="typeSpeed"' in html
    assert 'id="typeSql"' in html
    assert 'id="thinkOff"' in html
    assert 'id="thinkOn"' in html
    assert 'id="reasoningEffort"' in html
    assert 'id="questionIds"' in html
    assert 'SQL benchmark uses backend-built prompts' in html


def test_frontend_has_sql_mode_payload_and_rendering_hooks():
    frontend = _read_frontend()

    assert 'function isSqlBenchmarkMode()' in frontend
    assert 'function selectedBenchmarkTypes()' in frontend
    assert 'function getThinkingMode()' in frontend
    assert 'function getReasoningEffort()' in frontend
    assert 'function parseQuestionIds(rawValue)' in frontend
    assert 'function renderSqlResults(results)' in frontend
    assert 'function updateBenchmarkModeUi()' in frontend
    assert 'function selectedSqlProvider()' in frontend
    assert 'function buildBenchmarkPayloads()' in frontend
    assert "benchmark_type: 'sql'" in frontend
    assert "benchmark_type: 'speed'" in frontend
    assert 'thinking_mode: thinkingMode' in frontend
    assert 'reasoning_effort: getReasoningEffort()' in frontend
    assert 'question_ids: parseQuestionIds' in frontend
    assert 'exactlyOneIncludedProvider' in frontend
    assert 'select at least one model' in frontend.lower()
    assert '/results.jsonl' in frontend
    assert '/results.csv' in frontend
    assert '/results.tsv' in frontend
    assert '/manifest.json' in frontend
    assert '/summary.json' in frontend
    assert 'download="${escapeHtml(item.job_id)}.results.jsonl"' in frontend
    assert 'download="${escapeHtml(item.job_id)}.results.csv"' in frontend
    assert 'download="${escapeHtml(item.job_id)}.results.tsv"' in frontend
    assert 'download="${escapeHtml(item.job_id)}.manifest.json"' in frontend
    assert 'download="${escapeHtml(item.job_id)}.summary.json"' in frontend


def test_frontend_history_and_summary_branch_by_benchmark_type():
    frontend = _read_frontend()

    assert 'Benchmark Type' in frontend
    assert "job.request?.benchmark_type" in frontend
    assert "renderResults(job, job.request?.benchmark_type || 'speed')" in frontend
    assert "updateSummary(job, job.request?.benchmark_type || 'speed')" in frontend


def test_frontend_conversation_renderer_uses_cards():
    frontend = _read_frontend()

    assert 'conv-thinking' in frontend
    assert 'conv-tool-call' in frontend
    assert 'conv-tool-result' in frontend
    assert 'THINKING' in frontend
    assert 'Tool call:' in frontend


def test_frontend_hides_empty_results_ok_tool_call_cards():
    frontend = _read_frontend()

    assert 'visibleToolCalls' in frontend
    assert "name === 'results_ok'" in frontend
    assert "rawArgs === '{}'" in frontend
    assert 'textContent || visibleToolCalls.length' in frontend


def test_frontend_history_shows_model_count_for_sql():
    frontend = _read_frontend()

    assert 'distinctModelCount' in frontend
    assert "benchmark_type === 'sql'" in frontend


def test_frontend_speed_table_uses_ttft_total_and_operation_status():
    frontend = _read_frontend()

    assert 'id="currentOperation"' in frontend
    assert 'Current operation:' in frontend
    assert 'Latency (s)' not in frontend
    assert 'Total (ms)' not in frontend
    # Raw table uses TTFT (s), aggregated table uses Avg TTFT (ms)
    assert 'Total (s)' in frontend
    assert 'TTFT (s)' in frontend
    assert "formatMillisecondsAsSeconds(result.total_time_ms)" in frontend
    assert "formatMillisecondsAsSeconds(result.ttft_ms)" in frontend
    assert "formatTps(result.prefill_tps)" in frontend
    assert "formatTps(result.decode_tps)" in frontend
    assert "current_message" in frontend
    assert "Best TTFT" in frontend
    # Aggregated view headers
    assert 'Avg TTFT (ms)' in frontend
    assert 'Avg Decode (tok/s)' in frontend


def test_frontend_quant_parser_recognizes_unsloth_dynamic_formats():
    frontend = _read_frontend()

    assert "UD_IQ1_S" in frontend
    assert "unslothDynamic" in frontend
    assert "UNSLOTH" in frontend
    assert "DYNAMIC" in frontend
