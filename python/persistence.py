"""On-disk record persistence: save, load, migrate, reconcile, clear, export.

All functions take either a ``BenchmarkServer`` (for state like
``results_store_dir`` and the per-server lock) or the path/lock directly.
This module owns the canonical record schema written to
``benchmarks/<inverted_ts>_<job_id>.json`` files.

The inverted-timestamp filename (``_inverted_prefix``) is 13 digits wide
and pads ``(BASE - epoch_ms)`` so a plain ascending sort lists newest
first in any file manager. ``BASE = 9_999_999_999_999`` ms is ~year 2286.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from python.json_io import _load_json, ts_utc

if TYPE_CHECKING:  # pragma: no cover
    from python.benchmark_server import BenchmarkServer
    from python.models import JobState

LOG = logging.getLogger("llm_testbench")

# Base for inverted-timestamp filename prefixes. 9_999_999_999_999 ms is ~year
# 2286, comfortably beyond any real run, so (BASE - epoch_ms) stays positive and
# 13 digits wide. Newer runs have a smaller prefix, so a plain ascending sort of
# filenames (the OS/explorer default) lists newest first.
_FILENAME_TS_BASE = 9_999_999_999_999


def inverted_prefix(created_at_iso: Optional[str]) -> str:
    """Return a zero-padded inverted-timestamp prefix for ordering filenames."""
    epoch_ms = 0
    if created_at_iso:
        try:
            dt = datetime.fromisoformat(created_at_iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            epoch_ms = int(dt.timestamp() * 1000)
        except (ValueError, TypeError):
            epoch_ms = 0
    inverted = _FILENAME_TS_BASE - epoch_ms
    if inverted < 0:
        inverted = 0
    return f"{inverted:013d}"


def record_filename(server: "BenchmarkServer", job_id: str, created_at_iso: Optional[str]) -> str:
    """Filename for a job record: '<inverted_ts>_<job_id>.json'."""
    return f"{inverted_prefix(created_at_iso)}_{job_id}.json"


def find_record_path(server: "BenchmarkServer", job_id: str) -> Optional[Path]:
    """Locate an existing record file for job_id (new scheme or legacy)."""
    results_dir = server.results_store_dir
    if not results_dir.exists():
        return None
    # New scheme: <prefix>_<job_id>.json
    matches = list(results_dir.glob(f"*_{job_id}.json"))
    if matches:
        return matches[0]
    # Legacy scheme: <job_id>.json
    legacy = results_dir / f"{job_id}.json"
    return legacy if legacy.exists() else None


async def load_results_store(server: "BenchmarkServer") -> List[Dict[str, Any]]:
    async with server._results_lock:
        results_dir = server.results_store_dir
        if not results_dir.exists():
            return []
        try:
            paths = sorted(results_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            return []
        items: List[Dict[str, Any]] = []
        for path in paths:
            try:
                content = await asyncio.to_thread(path.read_text, "utf-8")
                data = json.loads(content)
                if isinstance(data, dict):
                    items.append(data)
            except (OSError, ValueError) as exc:
                LOG.warning("Failed reading result file %s: %s", path, exc)
        return items


async def migrate_legacy_filenames(server: "BenchmarkServer") -> int:
    """Rename legacy '<job_id>.json' records to the inverted-timestamp scheme.

    Run once at startup so old runs sort correctly alongside new ones. A
    legacy file is one whose stem is a bare job_id (no inverted prefix).
    """
    results_dir = server.results_store_dir
    migrated = 0
    async with server._results_lock:
        if not results_dir.exists():
            return 0
        for path in list(results_dir.glob("*.json")):
            name = path.name
            # New-scheme files start with a 13-digit prefix + '_'. Skip those.
            stem = name[:-5]  # strip '.json'
            prefix = stem.split("_", 1)[0]
            if len(prefix) == 13 and prefix.isdigit():
                continue
            try:
                data = _load_json(path)
            except (OSError, ValueError):
                continue
            job_id = data.get("job_id") or stem
            new_name = record_filename(server, job_id, data.get("created_at"))
            if new_name == name:
                continue
            target = results_dir / new_name
            try:
                if target.exists():
                    path.unlink()  # a new-scheme copy already exists
                else:
                    path.rename(target)
                migrated += 1
            except OSError as exc:
                LOG.warning("Failed migrating legacy record %s: %s", path, exc)
    if migrated:
        LOG.info("Migrated %d legacy benchmark filename(s) to timestamp scheme", migrated)
    return migrated


async def reconcile_stale_records(server: "BenchmarkServer") -> int:
    """Mark on-disk records stuck in a live status as 'interrupted'.

    After a server restart, in-memory jobs are gone but their last-flushed
    disk record may still say 'running' (the finalizing `finally` never ran).
    Such records would otherwise show as a forever-running, unstoppable job in
    history. We rewrite them to 'interrupted' once at startup. Only records
    with no corresponding live in-memory job are touched.
    """
    live_statuses = {"queued", "running", "stopping"}
    results_dir = server.results_store_dir
    fixed = 0
    async with server._results_lock:
        if not results_dir.exists():
            return 0
        for path in results_dir.glob("*.json"):
            try:
                content = await asyncio.to_thread(path.read_text, "utf-8")
                data = json.loads(content)
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            job_id = data.get("job_id")
            if data.get("status") in live_statuses and job_id not in server.jobs:
                data["status"] = "interrupted"
                if not data.get("finished_at"):
                    data["finished_at"] = ts_utc()
                try:
                    await asyncio.to_thread(
                        path.write_text, json.dumps(data, ensure_ascii=False, indent=2), "utf-8"
                    )
                    fixed += 1
                except OSError as exc:
                    LOG.warning("Failed reconciling stale record %s: %s", path, exc)
    if fixed:
        LOG.info("Reconciled %d stale 'running' record(s) -> 'interrupted'", fixed)
    return fixed


async def save_job_record(server: "BenchmarkServer", job_id: str, record: Dict[str, Any]) -> None:
    results_dir = server.results_store_dir
    results_dir.mkdir(parents=True, exist_ok=True)
    filename = record_filename(server, job_id, record.get("created_at"))
    path = results_dir / filename
    # Remove any stale file for this job under a different name (legacy
    # <job_id>.json or an older prefix), so incremental saves don't leave
    # duplicates behind. The directory scan itself is serialized by
    # ``_results_lock`` so two jobs don't trample each other's globs.
    async with server._results_lock:
        for other in results_dir.glob(f"*_{job_id}.json"):
            if other.name != filename:
                try: other.unlink()
                except OSError: pass
        legacy = results_dir / f"{job_id}.json"
        if legacy.exists() and legacy.name != filename:
            try: legacy.unlink()
            except OSError: pass
    payload = json.dumps(record, ensure_ascii=False, indent=2)
    await asyncio.to_thread(path.write_text, payload, "utf-8")


async def flush_job_record(server: "BenchmarkServer", job: "JobState") -> None:
    """Write current job state to disk (called after each question for incremental saves).

    The per-job lock serialises "snapshot job.results" + "write record"
    for the SAME job. Saves for different jobs proceed in parallel.
    """
    async with job._save_lock:
        await append_job_to_results_store(server, job)


async def append_job_to_results_store(server: "BenchmarkServer", job: "JobState") -> None:
    record = {
        "job_id": job.job_id,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "status": job.status,
        "request": {
            "benchmark_type": job.request.benchmark_type,
            "base_url": job.request.base_url,
            "provider": job.request.provider,
            "models": job.request.models,
            "mode": job.request.mode,
            "concurrency": job.request.concurrency,
            "max_tokens": job.request.max_tokens,
            "temperature": job.request.temperature,
            "top_p": job.request.top_p,
            "presence_penalty": job.request.presence_penalty,
            "frequency_penalty": job.request.frequency_penalty,
            "timeout_ms": job.request.timeout_ms,
            "repeat_count": job.request.repeat_count,
            "warmup_runs": job.request.warmup_runs,
            "max_concurrent_predictions": job.request.max_concurrent_predictions,
            "mtp": job.request.mtp,
            "k_cache_quantization": job.request.k_cache_quantization,
            "v_cache_quantization": job.request.v_cache_quantization,
            "batch_size": job.request.batch_size,
            "flash_attn": job.request.flash_attn,
            "question_ids": job.request.question_ids,
            "sql_mode": job.request.sql_mode,
            "thinking_mode": job.request.thinking_mode,
            "reasoning_effort": job.request.reasoning_effort,
            "question_timeout_ms": job.request.question_timeout_ms,
        },
        "progress": {
            "completed": job.progress_completed,
            "total": job.progress_total,
            "current_model": job.current_model,
            "current_provider_id": getattr(job, "current_provider_id", None),
            "current_provider_label": getattr(job, "current_provider_label", None),
        },
        "results": job.results,
        "errors": job.errors,
        "report_text": job.report_text,
    }
    await save_job_record(server, job.job_id, record)


async def load_job_record(
    server: "BenchmarkServer",
    job_id: str,
    *,
    log_label: str,
) -> Optional[Dict[str, Any]]:
    """Resolve a job_id to a record: live in-memory job first, then on-disk."""
    live_job = server.jobs.get(job_id)
    if live_job is not None:
        return live_job.to_dict()
    path = find_record_path(server, job_id)
    if path and path.exists():
        try:
            content = await asyncio.to_thread(path.read_text, "utf-8")
            loaded = json.loads(content)
            if isinstance(loaded, dict):
                return loaded
        except (OSError, ValueError) as exc:
            LOG.warning("Failed reading %s export record %s: %s", log_label, path, exc)
    return None


def build_results_jsonl(record: Dict[str, Any]) -> str:
    request_meta = record.get("request") if isinstance(record.get("request"), dict) else {}
    progress_meta = record.get("progress") if isinstance(record.get("progress"), dict) else {}
    results = record.get("results") if isinstance(record.get("results"), list) else []
    lines: List[str] = []
    for index, result in enumerate(results):
        row = {
            "job_id": record.get("job_id"),
            "status": record.get("status"),
            "created_at": record.get("created_at"),
            "started_at": record.get("started_at"),
            "finished_at": record.get("finished_at"),
            "benchmark_type": request_meta.get("benchmark_type") or (result.get("benchmark_type") if isinstance(result, dict) else None),
            "request": request_meta,
            "progress": progress_meta,
            "result_index": index,
            "result": result,
        }
        lines.append(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
    return "\n".join(lines) + ("\n" if lines else "")


def build_results_table(record: Dict[str, Any], *, delimiter: str = ",") -> str:
    request_meta = record.get("request") if isinstance(record.get("request"), dict) else {}
    results = record.get("results") if isinstance(record.get("results"), list) else []
    fieldnames = [
        "job_id",
        "status",
        "benchmark_type",
        "result_index",
        "model",
        "provider",
        "provider_label",
        "endpoint",
        "outcome",
        "success",
        "error",
        "latency_s",
        "total_time_s",
        "prompt_tokens",
        "completion_tokens",
        "tokens_per_second",
        "question_id",
        "difficulty",
        "thinking_mode",
        "reasoning_effort",
        "reasoning_fallback",
        "generated_sql",
        "expected_sql",
        "result_json",
    ]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, delimiter=delimiter, lineterminator="\n")
    writer.writeheader()
    for index, result in enumerate(results):
        item = result if isinstance(result, dict) else {}
        writer.writerow({
            "job_id": record.get("job_id"),
            "status": record.get("status"),
            "benchmark_type": request_meta.get("benchmark_type") or item.get("benchmark_type"),
            "result_index": index,
            "model": item.get("model"),
            "provider": item.get("provider"),
            "provider_label": item.get("provider_label"),
            "endpoint": item.get("endpoint"),
            "outcome": item.get("outcome") or item.get("status"),
            "success": item.get("success"),
            "error": item.get("error"),
            "latency_s": item.get("latency_s") or item.get("latency"),
            "total_time_s": item.get("total_time_s") or item.get("total_time"),
            "prompt_tokens": item.get("prompt_tokens") or item.get("input_tokens"),
            "completion_tokens": item.get("completion_tokens") or item.get("output_tokens"),
            "tokens_per_second": item.get("tokens_per_second"),
            "question_id": item.get("question_id"),
            "difficulty": item.get("difficulty"),
            "thinking_mode": item.get("thinking_mode"),
            "reasoning_effort": item.get("reasoning_effort"),
            "reasoning_fallback": item.get("reasoning_fallback"),
            "generated_sql": item.get("generated_sql"),
            "expected_sql": item.get("expected_sql"),
            "result_json": json.dumps(result, ensure_ascii=False, separators=(",", ":")),
        })
    return buffer.getvalue()


def build_results_csv(record: Dict[str, Any]) -> str:
    return build_results_table(record, delimiter=",")


def build_results_tsv(record: Dict[str, Any]) -> str:
    return build_results_table(record, delimiter="\t")


def build_run_manifest(record: Dict[str, Any]) -> str:
    request_meta = record.get("request") if isinstance(record.get("request"), dict) else {}
    progress_meta = record.get("progress") if isinstance(record.get("progress"), dict) else {}
    results = record.get("results") if isinstance(record.get("results"), list) else []
    errors = record.get("errors") if isinstance(record.get("errors"), list) else []
    models = sorted({
        str(item.get("model"))
        for item in results
        if isinstance(item, dict) and item.get("model")
    })
    providers = sorted({
        str(item.get("provider_label") or item.get("provider") or item.get("endpoint"))
        for item in results
        if isinstance(item, dict) and (item.get("provider_label") or item.get("provider") or item.get("endpoint"))
    })
    outcomes: Dict[str, int] = {}
    for item in results:
        if not isinstance(item, dict):
            continue
        key = str(item.get("outcome") or item.get("status") or "unknown")
        outcomes[key] = outcomes.get(key, 0) + 1
    manifest = {
        "schema_version": 1,
        "generated_at": ts_utc(),
        "job_id": record.get("job_id"),
        "status": record.get("status"),
        "created_at": record.get("created_at"),
        "started_at": record.get("started_at"),
        "finished_at": record.get("finished_at"),
        "benchmark_type": request_meta.get("benchmark_type"),
        "request": request_meta,
        "progress": progress_meta,
        "result_count": len(results),
        "error_count": len(errors),
        "models": models,
        "providers": providers,
        "outcomes": outcomes,
        "export_endpoints": {
            "jsonl": f"/api/benchmark/{record.get('job_id')}/results.jsonl",
            "csv": f"/api/benchmark/{record.get('job_id')}/results.csv",
            "tsv": f"/api/benchmark/{record.get('job_id')}/results.tsv",
            "manifest": f"/api/benchmark/{record.get('job_id')}/manifest.json",
            "summary": f"/api/benchmark/{record.get('job_id')}/summary.json",
        },
    }
    return json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"


def build_run_summary(record: Dict[str, Any]) -> str:
    def number(value: Any) -> Optional[float]:
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def first_number(*values: Any) -> Optional[float]:
        for value in values:
            parsed = number(value)
            if parsed is not None:
                return parsed
        return None

    def average(values: List[float]) -> Optional[float]:
        if not values:
            return None
        return round(sum(values) / len(values), 4)

    request_meta = record.get("request") if isinstance(record.get("request"), dict) else {}
    progress_meta = record.get("progress") if isinstance(record.get("progress"), dict) else {}
    results = record.get("results") if isinstance(record.get("results"), list) else []
    errors = record.get("errors") if isinstance(record.get("errors"), list) else []
    rows = [item for item in results if isinstance(item, dict)]

    successes = [item for item in rows if item.get("success") is True]
    failures = [item for item in rows if item.get("success") is False]
    latency_values = [
        value
        for value in (first_number(item.get("latency_ms"), item.get("latency_s")) for item in rows)
        if value is not None
    ]
    total_time_values = [
        value
        for value in (first_number(item.get("total_time_ms"), item.get("total_time_s")) for item in rows)
        if value is not None
    ]
    ttft_values = [
        value
        for value in (number(item.get("ttft_ms")) for item in rows)
        if value is not None
    ]
    decode_tps_values = [
        value
        for value in (first_number(item.get("decode_tps"), item.get("tokens_per_second")) for item in rows)
        if value is not None
    ]
    total_prompt_tokens = sum(
        int(value)
        for value in (first_number(item.get("prompt_tokens"), item.get("input_tokens")) for item in rows)
        if value is not None
    )
    total_completion_tokens = sum(
        int(value)
        for value in (first_number(item.get("completion_tokens"), item.get("output_tokens")) for item in rows)
        if value is not None
    )
    total_cost = round(sum(
        value
        for value in (number(item.get("cost")) for item in rows)
        if value is not None
    ), 8)

    by_model: Dict[str, Dict[str, Any]] = {}
    for item in rows:
        model = str(item.get("model") or "unknown")
        bucket = by_model.setdefault(model, {
            "count": 0,
            "pass_count": 0,
            "fail_count": 0,
            "latency_ms_values": [],
            "total_cost": 0.0,
        })
        bucket["count"] += 1
        if item.get("success") is True:
            bucket["pass_count"] += 1
        elif item.get("success") is False:
            bucket["fail_count"] += 1
        latency = first_number(item.get("latency_ms"), item.get("latency_s"))
        if latency is not None:
            bucket["latency_ms_values"].append(latency)
        cost = number(item.get("cost"))
        if cost is not None:
            bucket["total_cost"] += cost

    model_summary: Dict[str, Dict[str, Any]] = {}
    for model, bucket in sorted(by_model.items()):
        count = bucket["count"]
        model_summary[model] = {
            "count": count,
            "pass_count": bucket["pass_count"],
            "fail_count": bucket["fail_count"],
            "pass_rate": round(bucket["pass_count"] / count, 4) if count else None,
            "avg_latency_ms": average(bucket["latency_ms_values"]),
            "total_cost": round(bucket["total_cost"], 8),
        }

    result_count = len(rows)
    summary = {
        "schema_version": 1,
        "generated_at": ts_utc(),
        "job_id": record.get("job_id"),
        "status": record.get("status"),
        "benchmark_type": request_meta.get("benchmark_type"),
        "request": request_meta,
        "progress": progress_meta,
        "result_count": result_count,
        "pass_count": len(successes),
        "fail_count": len(failures),
        "pass_rate": round(len(successes) / result_count, 4) if result_count else None,
        "error_count": len(errors),
        "latency": {
            "avg_latency_ms": average(latency_values),
            "avg_total_time_ms": average(total_time_values),
            "avg_ttft_ms": average(ttft_values),
            "avg_decode_tps": average(decode_tps_values),
        },
        "tokens": {
            "prompt_tokens": total_prompt_tokens,
            "completion_tokens": total_completion_tokens,
            "total_tokens": total_prompt_tokens + total_completion_tokens,
        },
        "cost": {
            "total": total_cost,
        },
        "models": model_summary,
    }
    return json.dumps(summary, ensure_ascii=False, indent=2) + "\n"


async def clear_results(
    server: "BenchmarkServer",
    job_ids: List[str],
    clear_all: bool,
) -> Dict[str, int]:
    """Delete record files for a list of job_ids, or all of them.

    Returns ``{"removed": int, "remaining": int}``.
    """
    job_ids = {str(item).strip() for item in job_ids if str(item).strip()}
    results_dir = server.results_store_dir
    async with server._results_lock:
        if clear_all:
            paths = list(results_dir.glob("*.json")) if results_dir.exists() else []
            removed = len(paths)
            for path in paths:
                try:
                    path.unlink(missing_ok=True)
                except OSError as exc:
                    LOG.warning("Failed deleting result file %s: %s", path, exc)
        else:
            if not job_ids:
                raise ValueError("Provide job_ids or set all=true")
            removed = 0
            for job_id in job_ids:
                path = find_record_path(server, job_id)
                if path and path.exists():
                    try:
                        path.unlink()
                        removed += 1
                    except OSError as exc:
                        LOG.warning("Failed deleting result file %s: %s", path, exc)
    remaining_count = len(list(results_dir.glob("*.json"))) if results_dir.exists() else 0
    return {"removed": removed, "remaining": remaining_count}
