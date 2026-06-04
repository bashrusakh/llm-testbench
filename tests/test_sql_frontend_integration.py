from pathlib import Path


INDEX_HTML = Path(__file__).resolve().parents[1] / "index.html"


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
    html = INDEX_HTML.read_text("utf-8")

    assert 'function isSqlBenchmarkMode()' in html
    assert 'function selectedBenchmarkTypes()' in html
    assert 'function getThinkingMode()' in html
    assert 'function getReasoningEffort()' in html
    assert 'function parseQuestionIds(rawValue)' in html
    assert 'function renderSqlResults(results)' in html
    assert 'function updateBenchmarkModeUi()' in html
    assert 'function selectedSqlProvider()' in html
    assert 'function buildBenchmarkPayloads()' in html
    assert "benchmark_type: 'sql'" in html
    assert "benchmark_type: 'speed'" in html
    assert 'thinking_mode: thinkingMode' in html
    assert 'reasoning_effort: getReasoningEffort()' in html
    assert 'question_ids: parseQuestionIds' in html
    assert 'exactlyOneIncludedProvider' in html
    assert 'select at least one model' in html.lower()
    assert '/results.jsonl' in html
    assert '/results.csv' in html
    assert '/results.tsv' in html
    assert '/manifest.json' in html
    assert 'download="${escapeHtml(item.job_id)}.results.jsonl"' in html
    assert 'download="${escapeHtml(item.job_id)}.results.csv"' in html
    assert 'download="${escapeHtml(item.job_id)}.results.tsv"' in html
    assert 'download="${escapeHtml(item.job_id)}.manifest.json"' in html


def test_frontend_history_and_summary_branch_by_benchmark_type():
    html = INDEX_HTML.read_text("utf-8")

    assert 'Benchmark Type' in html
    assert "job.request?.benchmark_type" in html
    assert "renderResults(job.results || [], job.request?.benchmark_type || 'speed')" in html
    assert "updateSummary(job, job.request?.benchmark_type || 'speed')" in html


def test_frontend_conversation_renderer_uses_cards():
    html = INDEX_HTML.read_text("utf-8")

    assert 'conv-thinking' in html
    assert 'conv-tool-call' in html
    assert 'conv-tool-result' in html
    assert 'THINKING' in html
    assert 'Tool call:' in html


def test_frontend_history_shows_model_count_for_sql():
    html = INDEX_HTML.read_text("utf-8")

    assert 'distinctModelCount' in html
    assert "benchmark_type === 'sql'" in html
