"""Unit tests for speed benchmark internals.

Covers the bits that PR #6 had to fix and that the existing
integration tests do not exercise directly:
- _compute_speed_aggregates: all-pass, mixed, all-failed branches
- _benchmark_openai: reasoning_content anchoring, fallback token counter,
  SSE error bodies, decoding window math
- _benchmark_ollama: response parsing and decode window
- _validate_endpoint_url: SSRF guard
"""

import asyncio
import json
import socket
from typing import Any, Dict, List

import pytest

import python.server as server_module
from python.server import (
    BenchmarkRequest,
    BenchmarkServer,
    BenchmarkTarget,
    JobState,
    _compute_speed_aggregates,
    _validate_endpoint_url,
)


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# _compute_speed_aggregates
# ---------------------------------------------------------------------------


def _run(model: str, run_index: int, success: bool, decode_tps=None, ttft_ms=None,
         completion_tokens=None, prompt_tokens=None, error: str = "") -> Dict[str, Any]:
    return {
        "model": model,
        "run_index": run_index,
        "success": success,
        "error": error,
        "decode_tps": decode_tps,
        "ttft_ms": ttft_ms,
        "total_time_ms": 100.0,
        "prefill_tps": None,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "provider_label": "lmstudio",
    }


def test_compute_speed_aggregates_aggregates_per_model():
    results = [
        _run("m", 1, True, decode_tps=10.0, ttft_ms=100.0),
        _run("m", 2, True, decode_tps=20.0, ttft_ms=200.0),
        _run("m", 3, True, decode_tps=30.0, ttft_ms=300.0),
    ]
    out = _compute_speed_aggregates(results)
    assert len(out) == 1
    agg = out[0]
    assert agg["model"] == "m"
    assert agg["success_count"] == 3
    assert agg["fail_count"] == 0
    assert agg["avg_decode_tps"] == 20.0
    assert agg["min_decode_tps"] == 10.0
    assert agg["max_decode_tps"] == 30.0
    assert agg["avg_ttft_ms"] == 200.0
    assert len(agg["runs"]) == 3
    assert {r["run_index"] for r in agg["runs"]} == {1, 2, 3}
    assert len(agg["runs"]) == 3  # actually populated, my bad in comment


def test_compute_speed_aggregates_includes_failed_only_models_when_others_succeed():
    """The PR #6 fix: failed-only models must still appear in the aggregated view."""
    results = [
        _run("winner", 1, True, decode_tps=25.0, ttft_ms=50.0),
        _run("winner", 2, True, decode_tps=27.0, ttft_ms=55.0),
        _run("loser", 1, False, error="boom"),
        _run("loser", 2, False, error="boom"),
    ]
    out = _compute_speed_aggregates(results)
    by_model = {a["model"]: a for a in out}
    assert set(by_model) == {"winner", "loser"}
    assert by_model["winner"]["success_count"] == 2
    assert by_model["winner"]["fail_count"] == 0
    assert by_model["loser"]["success_count"] == 0
    assert by_model["loser"]["fail_count"] == 2
    assert by_model["loser"]["avg_decode_tps"] is None
    assert by_model["loser"]["runs"] == []


def test_compute_speed_aggregates_all_failed_returns_each_model():
    results = [
        _run("a", 1, False, error="x"),
        _run("b", 1, False, error="y"),
    ]
    out = _compute_speed_aggregates(results)
    by_model = {a["model"]: a for a in out}
    assert set(by_model) == {"a", "b"}
    for agg in out:
        assert agg["success_count"] == 0
        assert agg["fail_count"] == 1
        assert agg["avg_decode_tps"] is None


def test_compute_speed_aggregates_excludes_warmup_runs():
    """run_index == 0 is the warmup run; should be filtered out."""
    results = [
        _run("m", 0, True, decode_tps=999.0),  # warmup, ignored
        _run("m", 1, True, decode_tps=10.0),
    ]
    out = _compute_speed_aggregates(results)
    assert len(out) == 1
    assert out[0]["success_count"] == 1
    assert out[0]["avg_decode_tps"] == 10.0


# ---------------------------------------------------------------------------
# _validate_endpoint_url — SSRF guard
# ---------------------------------------------------------------------------


def test_validate_endpoint_url_accepts_public_https():
    # Use a name that resolves to a public IP; we don't actually issue a request.
    _validate_endpoint_url("https://api.openai.com/v1")  # may resolve to public IPs


def test_validate_endpoint_url_accepts_loopback():
    """The benchmark server is local-only and its primary use case is
    benchmarking locally-running LLM servers (Ollama on 11434, LM Studio
    on 1234, llama.cpp on 8080). The guard must let loopback through."""
    _validate_endpoint_url("http://127.0.0.1:11434")
    _validate_endpoint_url("http://localhost:1234")
    _validate_endpoint_url("http://[::1]:8080")


def test_validate_endpoint_url_accepts_rfc1918():
    """Self-hosted OpenAI-compatible endpoints on a LAN are a common
    use case. RFC1918 (10/8, 172.16/12, 192.168/16) must be allowed."""
    _validate_endpoint_url("http://10.0.0.1:8000")
    _validate_endpoint_url("http://192.168.1.1")
    _validate_endpoint_url("http://172.16.0.1")


def test_validate_endpoint_url_rejects_link_local_metadata():
    """AWS / GCP / Azure instance metadata service. The real SSRF
    attack surface — must still be blocked."""
    with pytest.raises(ValueError, match="non-routable address"):
        _validate_endpoint_url("http://169.254.169.254/latest/meta-data")
    with pytest.raises(ValueError, match="non-routable address"):
        _validate_endpoint_url("http://[fe80::1]/")


def test_validate_endpoint_url_rejects_multicast_and_reserved():
    """Multicast, reserved, and unspecified addresses can never be a
    real LLM server. Reject them defensively."""
    with pytest.raises(ValueError, match="non-routable address"):
        _validate_endpoint_url("http://224.0.0.1:1234")
    with pytest.raises(ValueError, match="non-routable address"):
        _validate_endpoint_url("http://0.0.0.0/")


def test_validate_endpoint_url_rejects_bad_scheme():
    with pytest.raises(ValueError, match="scheme"):
        _validate_endpoint_url("file:///etc/passwd")
    with pytest.raises(ValueError, match="scheme"):
        _validate_endpoint_url("gopher://example.com")


# ---------------------------------------------------------------------------
# _benchmark_openai — SSE parsing
# ---------------------------------------------------------------------------


class FakeStreamResponse:
    """Minimal stand-in for httpx.Response used by client.stream(...)."""

    def __init__(self, status_code: int, lines: List[str]):
        self.status_code = status_code
        self._lines = lines
        self._aiter_calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self) -> bytes:
        return b""


class FakeStreamClient:
    """Captures the payload, returns canned SSE lines."""

    def __init__(self, response: FakeStreamResponse):
        self._response = response
        self.last_json: Dict[str, Any] = {}

    def __init_context(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, method: str, url: str, **kwargs):
        self.last_json = kwargs.get("json", {})
        return self._response


def _make_request(base_url: str = "http://api.example.com") -> BenchmarkRequest:
    return BenchmarkRequest.from_dict({
        "benchmark_type": "speed",
        "base_url": base_url,
        "api_key": "",
        "provider": "openai-compatible",
        "models": ["m"],
        "max_tokens": 64,
        "temperature": 0.0,
        "top_p": 1.0,
        "presence_penalty": 0.0,
        "frequency_penalty": 0.0,
        "timeout_ms": 30000,
        "prompt": "hi",
        "repeat_count": 1,
        "warmup_runs": 0,
        "mode": "sequential",
        "concurrency": 1,
    })


def _make_target(base_url: str = "http://api.example.com", provider: str = "openai-compatible") -> BenchmarkTarget:
    return BenchmarkTarget(
        provider_id="x",
        provider_label="x",
        base_url=base_url,
        api_key="",
        provider=provider,
        models=["m"],
    )


def _server() -> BenchmarkServer:
    return BenchmarkServer.__new__(BenchmarkServer)  # bypass __init__


def test_benchmark_openai_anchors_first_token_on_reasoning_content(monkeypatch):
    """Reasoning models stream reasoning_content before content. first_token_at
    must be set on the first reasoning chunk, not on the first content chunk,
    so the decode window covers the whole generation. Otherwise decode_tps
    is inflated by ~2x for typical R1/QwQ models (issue fixed in PR #6)."""
    monkeypatch.setattr(server_module, "_validate_endpoint_url", lambda *a, **kw: None)

    def make_lines() -> List[str]:
        chunks = [
            {"choices": [{"delta": {"role": "assistant"}}]},
            {"choices": [{"delta": {"reasoning_content": "Let me "}}]},
            {"choices": [{"delta": {"reasoning_content": "think."}}]},
            {"choices": [{"delta": {"content": "42"}}]},
            {"choices": [{"delta": {"content": " is the answer."}}], "usage": None},
            {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 5}},
            "data: [DONE]",
        ]
        return [f"data: {json.dumps(c)}" if isinstance(c, dict) else c for c in chunks]

    response = FakeStreamResponse(200, make_lines())
    fake = FakeStreamClient(response)

    spec = _make_request()
    target = _make_target()
    server = _server()
    server.http_client = fake
    result = run(server._benchmark_openai(spec, target, "m"))

    assert result["ttft_ms"] is not None
    assert result["completion_tokens"] == 5
    assert result["decode_tps"] is not None


def test_benchmark_openai_returns_none_when_usage_missing(monkeypatch):
    """Some backends ignore stream_options.include_usage. We must NOT derive a
    token count from chunk-counting — one chunk can contain many tokens, so the
    fake `decode_tps` it produced was off by N×. Better to surface None and let
    the UI render 'n/a' than to show a misleading number."""
    monkeypatch.setattr(server_module, "_validate_endpoint_url", lambda *a, **kw: None)

    def make_lines() -> List[str]:
        chunks = [
            {"choices": [{"delta": {"role": "assistant"}}]},
            {"choices": [{"delta": {"content": "Hello"}}]},
            {"choices": [{"delta": {"content": " world"}}]},
            {"choices": [{"delta": {"content": "!"}}]},
        ]
        return [f"data: {json.dumps(c)}" for c in chunks] + ["data: [DONE]"]

    response = FakeStreamResponse(200, make_lines())
    fake = FakeStreamClient(response)

    spec = _make_request()
    target = _make_target()
    server = _server()
    server.http_client = fake
    result = run(server._benchmark_openai(spec, target, "m"))

    assert result["completion_tokens"] is None
    assert result["decode_tps"] is None
    # TTFT is still measurable from the first non-empty delta even without usage.
    assert result["ttft_ms"] is not None


def test_benchmark_openai_raises_on_error_body_in_stream(monkeypatch):
    """An SSE chunk with top-level `error` must raise so the caller records
    success=False, not success=True with all metrics None."""
    monkeypatch.setattr(server_module, "_validate_endpoint_url", lambda *a, **kw: None)

    def make_lines() -> List[str]:
        return [
            f"data: {json.dumps(c)}"
            for c in [
                {"choices": [{"delta": {"role": "assistant"}}]},
                {"error": {"message": "model not found"}},
                "data: [DONE]",
            ]
        ]

    response = FakeStreamResponse(200, make_lines())
    fake = FakeStreamClient(response)

    spec = _make_request()
    target = _make_target()
    server = _server()
    server.http_client = fake
    with pytest.raises(RuntimeError, match="Server error in stream"):
        run(server._benchmark_openai(spec, target, "m"))


def test_benchmark_openai_allows_local_endpoint():
    """Local LLM servers (Ollama on 11434, LM Studio on 1234, llama.cpp
    on 8080) live on 127.0.0.1. The SSRF guard must NOT block them, so
    the benchmark proceeds to make the HTTP call."""

    class _BoomClient:
        def stream(self, *a, **kw):
            raise AssertionError("stream() reached: SSRF guard let the URL through")

    server = _server()
    server.http_client = _BoomClient()

    spec = _make_request()
    target = _make_target(base_url="http://127.0.0.1:11434")
    # Guard must NOT raise; http_client.stream() is reached and raises,
    # proving the guard let the URL through.
    with pytest.raises(AssertionError, match="stream\\(\\) reached"):
        run(server._benchmark_openai(spec, target, "m"))


def test_benchmark_openai_rejects_link_local_metadata(monkeypatch):
    """The cloud metadata endpoint (169.254.169.254) is the actual SSRF
    target and must still be blocked, even when supplied as base_url."""
    import python.server as server_module

    def fake_async_client(*a, **kw):
        raise AssertionError("AsyncClient must not be constructed for metadata URLs")

    monkeypatch.setattr(server_module.httpx, "AsyncClient", fake_async_client)

    spec = _make_request()
    target = _make_target(base_url="http://169.254.169.254/latest/meta-data")
    with pytest.raises(ValueError, match="non-routable address"):
        run(_server()._benchmark_openai(spec, target, "m"))


# ---------------------------------------------------------------------------
# _benchmark_ollama
# ---------------------------------------------------------------------------


class FakePostClient:
    def __init__(self, response_payload: Dict[str, Any], status_code: int = 200):
        self._payload = response_payload
        self._status = status_code

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url: str, **kwargs):
        self.last_json = kwargs.get("json", {})
        self.last_url = url

        class Resp:
            def __init__(self, status, payload):
                self.status_code = status
                self._payload = payload

            def json(self):
                return self._payload

            @property
            def text(self):
                return json.dumps(self._payload)

        return Resp(self._status, self._payload)


def test_benchmark_ollama_parses_eval_count_and_duration(monkeypatch):
    monkeypatch.setattr(server_module, "_validate_endpoint_url", lambda *a, **kw: None)
    payload = {
        "model": "m",
        "prompt_eval_count": 12,
        "eval_count": 30,
        "prompt_eval_duration": 1_000_000_000,  # 1s
        "eval_duration": 2_000_000_000,         # 2s
    }
    fake = FakePostClient(payload)

    spec = _make_request()
    target = _make_target(provider="ollama")
    server = _server()
    server.http_client = fake
    result = run(server._benchmark_ollama(spec, target, "m"))
    assert result["prompt_tokens"] == 12
    assert result["completion_tokens"] == 30
    assert result["ttft_ms"] == 1000.0
    assert result["decode_tps"] == 15.0  # 30 / 2s


def test_benchmark_ollama_returns_none_when_eval_count_missing(monkeypatch):
    monkeypatch.setattr(server_module, "_validate_endpoint_url", lambda *a, **kw: None)
    payload = {"model": "m"}
    fake = FakePostClient(payload)
    spec = _make_request()
    target = _make_target(provider="ollama")
    server = _server()
    server.http_client = fake
    result = run(server._benchmark_ollama(spec, target, "m"))
    assert result["decode_tps"] is None
    assert result["completion_tokens"] is None


# ---------------------------------------------------------------------------
# Ollama latency_ms regression (PR C bug fix)
# ---------------------------------------------------------------------------


def test_ollama_latency_ms_equals_total_time_ms(monkeypatch):
    """Regression: latency_ms used to be set to ttft_ms (prefill time),
    not the end-to-end time. OpenAI path returns latency_ms == total_time_ms;
    Ollama must match for cross-provider comparison and row-shape parity."""
    monkeypatch.setattr(server_module, "_validate_endpoint_url", lambda *a, **kw: None)
    payload = {
        "model": "m",
        "prompt_eval_count": 10,
        "eval_count": 20,
        "prompt_eval_duration": 500_000_000,   # 0.5s
        "eval_duration": 1_500_000_000,        # 1.5s
    }
    fake = FakePostClient(payload)
    spec = _make_request()
    target = _make_target(provider="ollama")
    server = _server()
    server.http_client = fake
    result = run(server._benchmark_ollama(spec, target, "m"))
    # Total wall-clock is 0.5s + 1.5s = 2.0s = 2000ms
    assert result["total_time_ms"] == 2000.0
    assert result["latency_ms"] == 2000.0
    assert result["latency_ms"] == result["total_time_ms"]
    # ttft_ms is the prefill slice only
    assert result["ttft_ms"] == 500.0


# ---------------------------------------------------------------------------
# _load_json helper (PR C)
# ---------------------------------------------------------------------------


def test_load_json_raises_with_path(tmp_path):
    """A malformed JSON file must raise an error that names the file."""
    bad = tmp_path / "results.json"
    bad.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid JSON in results.json"):
        server_module._load_json(bad)


def test_read_json_raises_with_path(tmp_path):
    bad = tmp_path / "questions.json"
    bad.write_text("[1, 2, oops]", encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid JSON in questions.json"):
        server_module._read_json(bad)


def test_load_json_rejects_non_object(tmp_path):
    bad = tmp_path / "results.json"
    bad.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ValueError, match="expected object"):
        server_module._load_json(bad)


# ---------------------------------------------------------------------------
# set_phase atomicity (PR C)
# ---------------------------------------------------------------------------


def test_set_phase_is_atomic():
    """Concurrent reader + writer must never see a torn (phase, message)
    pair from a single set_phase call. Reference assignment in CPython
    is atomic, and the lock guarantees no half-update."""
    spec = _make_request()
    job = JobState(request=spec)

    async def main():
        async def writer():
            for i in range(50):
                await job.set_phase(f"phase_{i}", f"msg_{i}", model=f"m_{i}", run_index=i)

        async def reader():
            for _ in range(200):
                snap = job.get_phase_snapshot()
                suffix = snap.phase.split("_", 1)[-1] if snap.phase.startswith("phase_") else None
                if suffix is not None:
                    assert snap.message == f"msg_{suffix}", (
                        f"torn read: phase={snap.phase} but message={snap.message}"
                    )
                await asyncio.sleep(0)

        await asyncio.gather(writer(), reader(), reader(), reader(), reader())

    run(main())


# ---------------------------------------------------------------------------
# _compute_speed_aggregates cache (PR C)
# ---------------------------------------------------------------------------


def test_speed_aggregates_cached_per_job():
    """to_dict() calls _compute_speed_aggregates on every poll. With the
    cache, two consecutive calls with the same (n, last_timestamp) return
    the same list object."""
    spec = _make_request()
    job = JobState(request=spec)
    job.results = [_run("m", 1, True, decode_tps=10.0), _run("m", 2, True, decode_tps=20.0)]
    first = job.to_dict()["aggregated_speed"]
    second = job.to_dict()["aggregated_speed"]
    assert first is second
    # Adding a result invalidates the cache.
    job.results.append(_run("m", 3, True, decode_tps=30.0))
    third = job.to_dict()["aggregated_speed"]
    assert third is not first


# ---------------------------------------------------------------------------
# Pending save draining (PR C)
# ---------------------------------------------------------------------------


def test_pending_saves_flushed_on_shutdown(monkeypatch, tmp_path):
    """drain_pending_saves awaits every track_save()'d task. Used in
    the job's ``finally`` and in the server's on_cleanup hook so a
    SIGTERM does not drop the last incremental save."""
    spec = _make_request()
    job = JobState(request=spec)
    completed = []

    async def fake_save(duration: float):
        await asyncio.sleep(duration)
        completed.append(duration)

    async def main():
        t1 = asyncio.ensure_future(fake_save(0.05))
        t2 = asyncio.ensure_future(fake_save(0.05))
        job.track_save(t1)
        job.track_save(t2)
        # They haven't finished yet
        assert not t1.done()
        await job.drain_pending_saves()
        # Now both are done and the registry is empty
        assert t1.done() and t2.done()
        assert not job._pending_saves
        assert sorted(completed) == [0.05, 0.05]

    run(main())


def test_server_shutdown_drains_all_jobs(monkeypatch, tmp_path):
    """BenchmarkServer.shutdown awaits drain_pending_saves for every
    job currently tracked, so a clean exit does not lose in-flight writes."""
    spec = _make_request()
    job = JobState(request=spec)

    async def slow_save():
        await asyncio.sleep(0.02)

    async def main():
        t = asyncio.ensure_future(slow_save())
        job.track_save(t)
        server = BenchmarkServer(server_module.INDEX_HTML)
        server.jobs[job.job_id] = job
        await server.shutdown()
        assert t.done()

    run(main())


# ---------------------------------------------------------------------------
# _scan_candidates — automatic local endpoint discovery
# ---------------------------------------------------------------------------


def test_scan_candidates_finds_local_ollama_despite_ssrf_guard(monkeypatch):
    """Regression: the SSRF guard in _probe_provider used to reject every
    candidate URL in the local scan (they are all loopback by construction),
    so /api/endpoints/scan always returned {"endpoints": []}. The guard is
    only meant for user-supplied URLs in the OpenAI/Ollama benchmark paths.
    Make sure the scan can still report a live local Ollama on 11434.
    """
    server = BenchmarkServer.__new__(BenchmarkServer)
    # Pin the candidate grid to one host and two ports so the test is fast
    # and deterministic.
    server.host_candidates = ["127.0.0.1"]
    server.port_candidates = [11434, 1234]
    server.openai_models_path = "/v1/models"
    server.ollama_tags_path = "/api/tags"
    server.local_scan_connect_timeout_s = 0.5
    server.local_scan_read_timeout_s = 0.5

    async def fake_detect(self_or_url, *args, **kwargs):  # accepts self binding
        # The first positional arg may be self (method) or base_url.
        # The real call is self._detect_provider(base_url, ...) so base_url is args[0]
        # when the method is bound (self is implicit). When monkeypatched at the
        # class level, self is passed explicitly as the first arg.
        base_url = args[0] if args else kwargs.get("base_url", "")
        if base_url == "http://127.0.0.1:11434":
            return "ollama"
        return None

    # Patch on the instance to avoid passing self
    async def fake_detect_bound(base_url, requested_provider, api_key, client=None):
        if base_url == "http://127.0.0.1:11434":
            return "ollama"
        return None

    monkeypatch.setattr(server, "_detect_provider", fake_detect_bound)

    async def main():
        return await server._scan_candidates()

    endpoints = run(main())
    assert len(endpoints) == 1
    ep = endpoints[0]
    assert ep.base_url == "http://127.0.0.1:11434"
    assert ep.provider == "ollama"
    assert ep.reachable is True
    assert ep.models_path == "/api/tags"
