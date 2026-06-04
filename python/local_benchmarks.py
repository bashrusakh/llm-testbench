"""Small local fixture-backed benchmarks.

These helpers intentionally avoid external services and heavyweight execution.
They validate repo-owned fixture files and provide deterministic scoring helpers
for smoke tests and future lightweight UI wiring.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any, Dict, List


LOCAL_FIXTURE_SPECS: Dict[str, Dict[str, Any]] = {
    "coding-micro": {
        "path": "coding_data/tasks.jsonl",
        "required": {"id", "language", "prompt", "checks"},
    },
    "json-schema": {
        "path": "json_schema_data/tasks.jsonl",
        "required": {"id", "prompt", "schema"},
    },
    "prompt-replay": {
        "path": "prompt_replay_data/tasks.jsonl",
        "required": {"id", "prompt", "checks"},
    },
}


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path.name}:{line_number}: invalid JSON: {exc.msg}") from exc
            if not isinstance(item, dict):
                raise ValueError(f"{path.name}:{line_number}: expected JSON object")
            rows.append(item)
    return rows


def load_local_tasks(project_root: Path, module_id: str) -> List[Dict[str, Any]]:
    spec = LOCAL_FIXTURE_SPECS[module_id]
    return read_jsonl(project_root / str(spec["path"]))


def validate_local_fixtures(project_root: Path) -> List[str]:
    errors: List[str] = []
    for module_id, spec in LOCAL_FIXTURE_SPECS.items():
        path = project_root / str(spec["path"])
        if not path.exists():
            errors.append(f"{module_id}: missing {spec['path']}")
            continue
        try:
            rows = read_jsonl(path)
        except Exception as exc:
            errors.append(f"{module_id}: {exc}")
            continue
        if not rows:
            errors.append(f"{module_id}: fixture file is empty")
            continue
        required = set(spec["required"])
        seen_ids: set[str] = set()
        for item in rows:
            task_id = str(item.get("id") or "")
            if not required.issubset(item):
                errors.append(f"{module_id}: task {task_id or '<missing>'} is missing required fields")
                break
            if not task_id:
                errors.append(f"{module_id}: task id is required")
                break
            if task_id in seen_ids:
                errors.append(f"{module_id}: duplicate task id {task_id}")
                break
            seen_ids.add(task_id)
    return errors


def _json_type_matches(value: Any, expected_type: str) -> bool:
    if expected_type == "object":
        return isinstance(value, dict)
    if expected_type == "array":
        return isinstance(value, list)
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected_type == "boolean":
        return isinstance(value, bool)
    if expected_type == "null":
        return value is None
    return True


def score_json_schema_result(schema: Dict[str, Any], text: str) -> Dict[str, Any]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        return {"success": False, "error": f"invalid JSON: {exc.msg}"}
    expected_type = schema.get("type")
    if expected_type and not _json_type_matches(payload, str(expected_type)):
        return {"success": False, "error": f"expected {expected_type}"}
    required = [str(name) for name in schema.get("required", [])]
    if isinstance(payload, dict):
        missing = [name for name in required if name not in payload]
        if missing:
            return {"success": False, "error": f"missing required fields: {', '.join(missing)}"}
        properties = schema.get("properties", {})
        if isinstance(properties, dict):
            for name, prop_schema in properties.items():
                if name not in payload or not isinstance(prop_schema, dict):
                    continue
                prop_type = prop_schema.get("type")
                if prop_type and not _json_type_matches(payload[name], str(prop_type)):
                    return {"success": False, "error": f"{name}: expected {prop_type}"}
                enum = prop_schema.get("enum")
                if isinstance(enum, list) and payload[name] not in enum:
                    return {"success": False, "error": f"{name}: value outside enum"}
    return {"success": True, "error": ""}


def score_prompt_replay_result(checks: Dict[str, Any], text: str) -> Dict[str, Any]:
    lowered = text.lower()
    for phrase in checks.get("contains_all", []):
        if str(phrase).lower() not in lowered:
            return {"success": False, "error": f"missing phrase: {phrase}"}
    contains_any = [str(phrase).lower() for phrase in checks.get("contains_any", [])]
    if contains_any and not any(phrase in lowered for phrase in contains_any):
        return {"success": False, "error": "missing any accepted phrase"}
    for phrase in checks.get("forbidden", []):
        if str(phrase).lower() in lowered:
            return {"success": False, "error": f"forbidden phrase: {phrase}"}
    min_length = int(checks.get("min_length", 0))
    if min_length and len(text.strip()) < min_length:
        return {"success": False, "error": f"too short: expected at least {min_length} chars"}
    return {"success": True, "error": ""}


def score_coding_result(checks: Dict[str, Any], code: str) -> Dict[str, Any]:
    try:
        ast.parse(code)
    except SyntaxError as exc:
        return {"success": False, "error": f"syntax error: {exc.msg}"}
    for phrase in checks.get("required_substrings", []):
        if str(phrase) not in code:
            return {"success": False, "error": f"missing code fragment: {phrase}"}
    for phrase in checks.get("forbidden_substrings", []):
        if str(phrase) in code:
            return {"success": False, "error": f"forbidden code fragment: {phrase}"}
    return {"success": True, "error": ""}
