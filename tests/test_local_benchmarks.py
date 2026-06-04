from pathlib import Path

from python.local_benchmarks import (
    load_local_tasks,
    score_coding_result,
    score_json_schema_result,
    score_prompt_replay_result,
    validate_local_fixtures,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_local_fixture_sets_load_from_repo():
    assert len(load_local_tasks(PROJECT_ROOT, "coding-micro")) == 3
    assert len(load_local_tasks(PROJECT_ROOT, "json-schema")) == 3
    assert len(load_local_tasks(PROJECT_ROOT, "prompt-replay")) == 3
    assert validate_local_fixtures(PROJECT_ROOT) == []


def test_json_schema_scoring_accepts_valid_json_and_rejects_missing_fields():
    schema = {
        "type": "object",
        "required": ["city", "unit"],
        "properties": {
            "city": {"type": "string"},
            "unit": {"type": "string", "enum": ["celsius"]},
        },
    }

    assert score_json_schema_result(schema, '{"city":"Paris","unit":"celsius"}')["success"] is True
    missing = score_json_schema_result(schema, '{"city":"Paris"}')
    assert missing["success"] is False
    assert "missing" in missing["error"]


def test_prompt_replay_scoring_uses_required_optional_and_forbidden_phrases():
    checks = {
        "contains_all": ["local"],
        "contains_any": ["fast", "small"],
        "forbidden": ["cloud service"],
        "min_length": 10,
    }

    assert score_prompt_replay_result(checks, "A local and fast benchmark.")["success"] is True
    forbidden = score_prompt_replay_result(checks, "A local fast cloud service benchmark.")
    assert forbidden["success"] is False
    assert "forbidden" in forbidden["error"]


def test_coding_scoring_checks_python_syntax_and_static_fragments():
    checks = {
        "required_substrings": ["def add(", "return"],
        "forbidden_substrings": ["input("],
    }

    assert score_coding_result(checks, "def add(a, b):\n    return a + b\n")["success"] is True
    syntax = score_coding_result(checks, "def add(:\n")
    assert syntax["success"] is False
    assert "syntax" in syntax["error"]
