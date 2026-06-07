"""Time + JSON I/O helpers.

``ts_utc`` is the canonical timestamp string used in every record.
``_load_json`` / ``_read_json`` add a path to the error message so log
output and test failures name the file that failed to parse.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict


def ts_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: Path) -> Dict[str, Any]:
    """Read a JSON file and re-raise decode errors with the path in the message.

    A bare ``json.loads(path.read_text(...))`` produces
    ``json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)``
    with no hint of which file is bad. This wraps it so log output and test
    failures name the file.
    """
    loaded = _read_json(path)
    if not isinstance(loaded, dict):
        raise ValueError(f"Invalid JSON in {path.name}: expected object, got {type(loaded).__name__}")
    return loaded


def _read_json(path: Path) -> Any:
    """Read any JSON value from a file with a path-tagged error on failure."""
    try:
        return json.loads(path.read_text("utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {path.name}: {exc}") from exc
