"""Per-model speed-run aggregation.

Pure function over a list of result rows. Lives in its own module so
``JobState._aggregated_speed`` (in models.py) can import it lazily without
dragging in the rest of server.py, and so its behaviour is testable in
isolation.
"""

from __future__ import annotations

from typing import Any, Dict, List


def _compute_speed_aggregates(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Aggregate successful non-warmup speed runs by model.

    Returns list of dicts with per-model statistics and raw run data for sparklines.
    """
    if not results:
        return []

    # Filter: successful runs only, exclude warmup (run_index > 0)
    successful_runs = [
        r for r in results
        if r.get("success") is True and (r.get("run_index") or 0) > 0
    ]
    failed_runs = [
        r for r in results
        if r.get("success") is False and (r.get("run_index") or 0) > 0
    ]

    if not successful_runs:
        # No successful runs, return minimal info for failed models
        models_seen = set()
        for r in failed_runs:
            models_seen.add(r.get("model"))
        return [
            {
                "model": model,
                "provider_label": next((r.get("provider_label") for r in failed_runs if r.get("model") == model), ""),
                "run_count": 0,
                "success_count": 0,
                "fail_count": sum(1 for r in failed_runs if r.get("model") == model),
                "avg_decode_tps": None,
                "min_decode_tps": None,
                "max_decode_tps": None,
                "avg_ttft_ms": None,
                "min_ttft_ms": None,
                "max_ttft_ms": None,
                "avg_total_time_ms": None,
                "avg_prefill_tps": None,
                "runs": [],
            }
            for model in sorted(models_seen)
        ]

    # Group successful runs by model
    by_model: Dict[str, List[Dict[str, Any]]] = {}
    for r in successful_runs:
        model = r.get("model", "unknown")
        by_model.setdefault(model, []).append(r)

    aggregates = []
    for model, runs in sorted(by_model.items()):
        # Sort runs by run_index for consistent ordering
        runs_sorted = sorted(runs, key=lambda x: x.get("run_index", 0))

        decode_values = [r.get("decode_tps") for r in runs_sorted if r.get("decode_tps") is not None]
        ttft_values = [r.get("ttft_ms") for r in runs_sorted if r.get("ttft_ms") is not None]
        total_time_values = [r.get("total_time_ms") for r in runs_sorted if r.get("total_time_ms") is not None]
        prefill_values = [r.get("prefill_tps") for r in runs_sorted if r.get("prefill_tps") is not None]

        # Raw run data for sparkline / expandable detail
        run_data = [
            {
                "run_index": r.get("run_index"),
                "decode_tps": r.get("decode_tps"),
                "ttft_ms": r.get("ttft_ms"),
                "total_time_ms": r.get("total_time_ms"),
                "prefill_tps": r.get("prefill_tps"),
                "prompt_tokens": r.get("prompt_tokens"),
                "completion_tokens": r.get("completion_tokens"),
            }
            for r in runs_sorted
        ]

        fail_count = sum(1 for r in failed_runs if r.get("model") == model)

        aggregates.append({
            "model": model,
            "provider_label": runs_sorted[0].get("provider_label", ""),
            "run_count": len(runs_sorted) + fail_count,
            "success_count": len(runs_sorted),
            "fail_count": fail_count,
            "avg_decode_tps": round(sum(decode_values) / len(decode_values), 2) if decode_values else None,
            "min_decode_tps": round(min(decode_values), 2) if decode_values else None,
            "max_decode_tps": round(max(decode_values), 2) if decode_values else None,
            "avg_ttft_ms": round(sum(ttft_values) / len(ttft_values), 2) if ttft_values else None,
            "min_ttft_ms": round(min(ttft_values), 2) if ttft_values else None,
            "max_ttft_ms": round(max(ttft_values), 2) if ttft_values else None,
            "avg_total_time_ms": round(sum(total_time_values) / len(total_time_values), 2) if total_time_values else None,
            "avg_prefill_tps": round(sum(prefill_values) / len(prefill_values), 2) if prefill_values else None,
            "runs": run_data,
        })

    # Ensure models with only failed runs are still represented in the aggregated view.
    failed_only_models = sorted({
        r.get("model") for r in failed_runs
        if r.get("model") and r.get("model") not in by_model
    })
    for model in failed_only_models:
        model_failures = [r for r in failed_runs if r.get("model") == model]
        aggregates.append({
            "model": model,
            "provider_label": model_failures[0].get("provider_label", "") if model_failures else "",
            "run_count": len(model_failures),
            "success_count": 0,
            "fail_count": len(model_failures),
            "avg_decode_tps": None,
            "min_decode_tps": None,
            "max_decode_tps": None,
            "avg_ttft_ms": None,
            "min_ttft_ms": None,
            "max_ttft_ms": None,
            "avg_total_time_ms": None,
            "avg_prefill_tps": None,
            "runs": [],
        })

    return aggregates
