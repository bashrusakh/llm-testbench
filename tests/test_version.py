"""Tests for the runtime version resolver in python._version.

The resolver picks one of: $LLM_TESTBENCH_VERSION → git describe → VERSION
file → "dev". Each branch is exercised here without hitting real git or the
real env so the tests stay deterministic.
"""

from __future__ import annotations

import os

import pytest

from python import _version


@pytest.fixture(autouse=True)
def _clean_env_and_cache(monkeypatch):
    """Clear env var and module cache between tests so order doesn't matter."""
    monkeypatch.delenv("LLM_TESTBENCH_VERSION", raising=False)
    _version.reset_cache()
    yield
    _version.reset_cache()


def test_env_var_wins_over_git_and_file(monkeypatch):
    monkeypatch.setenv("LLM_TESTBENCH_VERSION", "v9.9.9-test")
    monkeypatch.setattr(_version, "_read_git", lambda: "v0.0.0")
    monkeypatch.setattr(_version, "_read_file", lambda: "v0.0.0")
    info = _version.get_version_info()
    assert info == {"version": "v9.9.9-test", "source": "env"}


def test_git_used_when_env_missing(monkeypatch):
    monkeypatch.setattr(_version, "_read_env", lambda: None)
    monkeypatch.setattr(_version, "_read_git", lambda: "v0.2.2")
    monkeypatch.setattr(_version, "_read_file", lambda: "v9.9.9-should-not-be-used")
    info = _version.get_version_info()
    assert info == {"version": "v0.2.2", "source": "git"}


def test_file_used_when_env_and_git_missing(monkeypatch):
    monkeypatch.setattr(_version, "_read_env", lambda: None)
    monkeypatch.setattr(_version, "_read_git", lambda: None)
    monkeypatch.setattr(_version, "_read_file", lambda: "v0.2.2-from-file")
    info = _version.get_version_info()
    assert info == {"version": "v0.2.2-from-file", "source": "file"}


def test_fallback_when_all_sources_empty(monkeypatch):
    monkeypatch.setattr(_version, "_read_env", lambda: None)
    monkeypatch.setattr(_version, "_read_git", lambda: None)
    monkeypatch.setattr(_version, "_read_file", lambda: None)
    info = _version.get_version_info()
    assert info == {"version": "dev", "source": "fallback"}


def test_result_is_cached_between_calls(monkeypatch):
    """Subsequent calls don't re-run resolvers — important so /api/version is cheap."""
    calls = {"git": 0}

    def fake_git():
        calls["git"] += 1
        return "v1.0.0"

    monkeypatch.setattr(_version, "_read_env", lambda: None)
    monkeypatch.setattr(_version, "_read_git", fake_git)
    _version.get_version_info()
    _version.get_version_info()
    _version.get_version_info()
    assert calls["git"] == 1


def test_empty_env_value_falls_through(monkeypatch):
    """Empty/whitespace env var must be treated as unset, not as the version."""
    monkeypatch.setenv("LLM_TESTBENCH_VERSION", "   ")
    monkeypatch.setattr(_version, "_read_git", lambda: "v0.2.2")
    info = _version.get_version_info()
    assert info["source"] == "git"
    assert info["version"] == "v0.2.2"


def test_read_file_handles_missing_file(monkeypatch, tmp_path):
    monkeypatch.setattr(_version, "_VERSION_FILE", tmp_path / "VERSION")
    assert _version._read_file() is None


def test_read_file_strips_whitespace(monkeypatch, tmp_path):
    path = tmp_path / "VERSION"
    path.write_text("  v0.5.0\n", encoding="utf-8")
    monkeypatch.setattr(_version, "_VERSION_FILE", path)
    assert _version._read_file() == "v0.5.0"
