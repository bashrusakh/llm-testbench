"""Resolve the running application's version without hardcoding it in code.

Resolution order, first non-empty wins:

1. ``LLM_TESTBENCH_VERSION`` environment variable — explicit override, useful
   for CI builds where ``git`` is missing but the release tag is known.
2. ``git describe --tags --abbrev=0`` in the project root — picks up the most
   recent annotated tag (e.g. ``v0.2.2``). Works for local dev clones and
   anywhere the ``.git`` directory is preserved.
3. ``VERSION`` file in the project root — GitHub Actions release workflow
   writes the release tag here before packaging the distribution. Survives
   ``git`` being absent (zip downloads of a release tarball).
4. ``"dev"`` — final fallback when nothing else is available.

The result is cached on the module so we don't fork ``git`` on every
``/api/version`` poll.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Dict, Optional

LOG = logging.getLogger("llm_testbench")

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_VERSION_FILE = _PROJECT_ROOT / "VERSION"
_ENV_VAR = "LLM_TESTBENCH_VERSION"
_FALLBACK = "dev"

# Cached (version, source) tuple. ``source`` is one of: env / git / file /
# fallback — exposed via /api/version so an operator can tell where the
# string came from without grepping logs.
_cache: Optional[Dict[str, str]] = None


def _read_env() -> Optional[str]:
    value = os.environ.get(_ENV_VAR, "").strip()
    return value or None


def _read_git() -> Optional[str]:
    """Return ``git describe --tags --abbrev=0`` output, or None if it fails.

    Suppresses every reasonable failure mode (no git binary, not a repo,
    no tags yet) — this is a best-effort lookup, not a hard dependency.
    """
    if not (_PROJECT_ROOT / ".git").exists():
        return None
    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--abbrev=0"],
            cwd=_PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def _read_file() -> Optional[str]:
    if not _VERSION_FILE.exists():
        return None
    try:
        value = _VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError as exc:
        LOG.warning("Failed reading VERSION file: %s", exc)
        return None
    return value or None


def _resolve() -> Dict[str, str]:
    for source, reader in (("env", _read_env), ("git", _read_git), ("file", _read_file)):
        value = reader()
        if value:
            return {"version": value, "source": source}
    return {"version": _FALLBACK, "source": "fallback"}


def get_version_info() -> Dict[str, str]:
    """Return ``{"version": ..., "source": ...}``, cached on first call."""
    global _cache
    if _cache is None:
        _cache = _resolve()
    return _cache


def get_version() -> str:
    """Just the version string."""
    return get_version_info()["version"]


def reset_cache() -> None:
    """Force re-resolution on the next call (used by tests)."""
    global _cache
    _cache = None
