"""Build the per-run row dict stored in JobState.results.

All speed runs (success, failure, stopped) write through this helper so
the column set is identical across outcomes. The success path passes
``metrics`` (output of ``_benchmark_openai`` / ``_benchmark_ollama``);
the failure path passes ``latency_ms`` and ``error="..."``. The stopped
path uses ``success="stopped"`` with no metrics.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Union

from python.models import BenchmarkTarget, JobState


def _build_speed_row(
    job: "JobState",
    target: "BenchmarkTarget",
    model: str,
    run_index: int,
    *,
    prompt_hash: str,
    timestamp: str,
    metrics: Optional[Dict[str, Any]] = None,
    success: Union[bool, str] = True,
    error: str = "",
    latency_ms: Optional[float] = None,
) -> Dict[str, Any]:
    """Build the row dict for a single speed benchmark attempt.

    The success path passes ``metrics`` (output of ``_benchmark_openai`` /
    ``_benchmark_ollama``); the failure path passes ``latency_ms`` and
    ``error="..."``. The stopped path uses ``success="stopped"`` with
    no metrics. All three paths share the same column set.
    """
    m = metrics or {}
    row: Dict[str, Any] = {
        "timestamp": timestamp,
        "job_id": job.job_id,
        "provider_id": target.provider_id,
        "provider_label": target.provider_label,
        "provider_type": target.provider,
        "endpoint": target.base_url,
        "provider": target.provider,
        "mode": job.request.mode,
        "model": model,
        "prompt_chars": len(job.request.prompt),
        "prompt_hash": prompt_hash,
        "max_tokens": job.request.max_tokens,
        "temperature": job.request.temperature,
        "top_p": job.request.top_p,
        "presence_penalty": job.request.presence_penalty,
        "frequency_penalty": job.request.frequency_penalty,
        "timeout_ms": job.request.timeout_ms,
        "run_index": run_index,
        "success": success,
        "error": error,
        "latency_ms": m.get("latency_ms") if metrics is not None else latency_ms,
        "total_time_ms": m.get("total_time_ms") if metrics is not None else latency_ms,
        "ttft_ms": m.get("ttft_ms") if metrics is not None else None,
        "prefill_tps": m.get("prefill_tps") if metrics is not None else None,
        "decode_tps": m.get("decode_tps") if metrics is not None else None,
        "prompt_tokens": m.get("prompt_tokens") if metrics is not None else None,
        "completion_tokens": m.get("completion_tokens") if metrics is not None else None,
        "completion_tokens_capped": m.get("completion_tokens_capped") if metrics is not None else None,
        "decode_tokens_measured": m.get("decode_tokens_measured") if metrics is not None else None,
        "warmup_runs": job.request.warmup_runs,
        "max_concurrent_predictions": job.request.max_concurrent_predictions,
        "mtp": job.request.mtp,
        "k_cache_quantization": job.request.k_cache_quantization,
        "v_cache_quantization": job.request.v_cache_quantization,
        "batch_size": job.request.batch_size,
        "flash_attn": job.request.flash_attn,
    }
    return row
