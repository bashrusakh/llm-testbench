"""Job execution: per-job orchestration, LLM calls, per-mode runners.

All functions take the ``BenchmarkServer`` (or a context object) as the
first arg where they need server state (HTTP clients, locks, registry
lookups). Pure functions like ``stopped_result`` and ``build_*_report``
don't.

The ``REASONING_FALLBACK_STATE`` ContextVar lives here because only
``call_llm_single`` / ``call_llm_tool_calling`` set and consume it.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import logging
import time
from typing import Any, Dict, List, Optional, TYPE_CHECKING

import httpx

from python.json_io import ts_utc
from python.models import BenchmarkTarget, JobState
from python.speed_row import _build_speed_row

if TYPE_CHECKING:  # pragma: no cover
    from python.benchmark_server import BenchmarkServer

LOG = logging.getLogger("llm_testbench")

REASONING_FALLBACK_STATE: contextvars.ContextVar[Optional[Dict[str, bool]]] = contextvars.ContextVar(
    "reasoning_fallback_state",
    default=None,
)

# Module-level constants mirror those in server.py. Kept here so job_runner
# is self-contained; the route layer imports them from server.py.
OPENAI_CHAT_PATH = "/v1/chat/completions"
OLLAMA_CHAT_PATH = "/api/chat"


def _coerce_message_content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_parts: List[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text")
                if isinstance(text, str):
                    text_parts.append(text)
            elif isinstance(item, str):
                text_parts.append(item)
        return "\n".join(part for part in text_parts if part)
    return str(content or "")


def _reasoning_payload_value(reasoning_effort: str) -> Optional[str]:
    effort = (reasoning_effort or "disabled").strip().lower()
    if effort in {"", "disabled"}:
        return None
    return effort


def _with_reasoning(payload: Dict[str, Any], reasoning_effort: str) -> Dict[str, Any]:
    effort = _reasoning_payload_value(reasoning_effort)
    if not effort:
        return payload
    enriched = dict(payload)
    enriched["reasoning"] = {"effort": effort}
    return enriched


def _looks_like_reasoning_rejection(status_code: int, body_text: str) -> bool:
    if status_code not in (400, 422):
        return False
    body_lower = body_text.lower()
    if "reasoning" in body_lower or "reasoning_effort" in body_lower:
        return True
    return "effort" in body_lower and any(
        keyword in body_lower
        for keyword in ("unknown", "unsupported", "not support", "extra", "invalid", "unrecognized")
    )


async def post_openai_chat_with_reasoning_fallback(
    client: httpx.AsyncClient,
    url: str,
    payload: Dict[str, Any],
    *,
    reasoning_effort: str,
    model: str,
    fallback_state: Optional[Dict[str, bool]] = None,
) -> httpx.Response:
    request_payload = _with_reasoning(payload, reasoning_effort)
    response = await client.post(url, json=request_payload)
    if "reasoning" not in request_payload:
        return response

    body_text = response.text if response.status_code in (400, 422) else ""
    if not _looks_like_reasoning_rejection(response.status_code, body_text):
        return response

    LOG.warning(
        "Model %s rejected reasoning effort %s (%s) — retrying without reasoning",
        model,
        reasoning_effort,
        response.status_code,
    )
    if fallback_state is None:
        fallback_state = REASONING_FALLBACK_STATE.get()
    if fallback_state is not None:
        fallback_state["used"] = True
    return await client.post(url, json=payload)


async def run_job(server: "BenchmarkServer", job: JobState) -> None:
    from python.persistence import append_job_to_results_store

    job.status = "running"
    job.started_at = ts_utc()
    await job.set_phase("starting", f"Starting {job.request.benchmark_type} benchmark")
    try:
        if job.request.benchmark_type == "sql":
            await run_sql_job(server, job)
        else:
            for target in job.request.targets:
                job.current_provider_id = target.provider_id
                job.current_provider_label = target.provider_label
                await job.set_phase("detecting_provider", f"Detecting provider for {target.provider_label}")
                normalized_provider = await server._detect_provider(target.base_url, target.provider, target.api_key)
                await job.set_phase("discovering_models", f"Discovering models at {target.provider_label}")
                available_models = await server._discover_models(target.base_url, normalized_provider, target.api_key)
                missing = [model for model in target.models if model not in available_models]
                if missing:
                    raise RuntimeError(f"Requested models not found for {target.provider_label}: {', '.join(missing)}")

                runtime_target = BenchmarkTarget(
                    provider_id=target.provider_id,
                    provider_label=target.provider_label,
                    base_url=target.base_url,
                    provider=normalized_provider,
                    api_key=target.api_key,
                    models=target.models,
                )
                if job.request.mode == "sequential":
                    await run_sequential(server, job, runtime_target)
                else:
                    await run_parallel(server, job, runtime_target)
                if job.stop_requested:
                    break

        if job.stop_requested and job.status == "stopping":
            job.status = "stopped"
            await job.set_phase("stopped", "Stopped")
        elif job.status not in {"failed", "stopped"}:
            job.status = "completed"
            await job.set_phase("completed", "Completed")
        job.report_text = build_report(job)
    except Exception as exc:
        LOG.exception("Benchmark job failed")
        job.errors.append(str(exc))
        job.status = "failed"
        await job.set_phase("failed", str(exc))
        job.report_text = build_report(job)
    finally:
        job.finished_at = ts_utc()
        # Wait for any in-flight incremental save tasks before the final write,
        # otherwise the last few results can be dropped if the process is killed.
        await job.drain_pending_saves()
        if job.results or job.errors:
            async with job._save_lock:
                await append_job_to_results_store(server, job)


async def run_sql_job(server: "BenchmarkServer", job: JobState) -> None:
    from python.persistence import flush_job_record
    from python.server import SQL_BENCHMARK_DATA_DIR  # lazy: avoid circular import
    # Lazy import of SqlBenchmarkRunner so test's
    # ``monkeypatch.setattr(server_module, "SqlBenchmarkRunner", FakeRunner)`` takes effect.
    from python.server import SqlBenchmarkRunner

    target = job.request.targets[0]
    normalized_provider = await server._detect_provider(target.base_url, target.provider, target.api_key)
    available_models = await server._discover_models(target.base_url, normalized_provider, target.api_key)

    requested_models = target.models
    missing = [m for m in requested_models if m not in available_models]
    if missing:
        raise RuntimeError(f"Requested model(s) not found for {target.provider_label}: {', '.join(missing)}")

    # Determine which thinking variants to run
    thinking_mode_req = job.request.thinking_mode
    if thinking_mode_req == "both":
        thinking_variants: List[str] = ["off", "on"]
    else:
        thinking_variants = [thinking_mode_req]

    runtime_target = BenchmarkTarget(
        provider_id=target.provider_id,
        provider_label=target.provider_label,
        base_url=target.base_url,
        provider=normalized_provider,
        api_key=target.api_key,
        models=requested_models,
    )
    job.current_provider_id = runtime_target.provider_id
    job.current_provider_label = runtime_target.provider_label

    with SqlBenchmarkRunner(
        llm_callback=lambda system, user, *, model, provider, endpoint, timeout_ms: call_llm_single(
            server,
            system,
            user,
            runtime_target,
            model,
            timeout_ms,
            reasoning_effort=job.request.reasoning_effort,
        ),
        data_dir=SQL_BENCHMARK_DATA_DIR,
    ) as runner:
        question_ids = job.request.question_ids
        if question_ids is None:
            question_ids = sorted(runner.questions_by_id)
        else:
            question_ids = [int(question_id) for question_id in question_ids]

        job.progress_total = len(requested_models) * len(thinking_variants) * len(question_ids)

        # Shared across all questions so each model's load is treated as cold
        # only once; the per-question timeout then excludes that load time.
        warmed_models: set = set()

        for model in requested_models:
            job.current_model = model
            for thinking_mode in thinking_variants:
                for question_id in question_ids:
                    if job.stop_requested:
                        return
                    reasoning_fallback_state = {"used": False}
                    fallback_token = REASONING_FALLBACK_STATE.set(reasoning_fallback_state)
                    try:
                        if job.request.sql_mode == "grammar":
                            result = await runner.run_question(
                                question_id=question_id,
                                model=model,
                                provider=runtime_target.provider,
                                endpoint=runtime_target.base_url,
                                timeout_ms=job.request.timeout_ms,
                                thinking_mode=thinking_mode,
                            )
                        else:
                            result = await runner.run_question_tool_calling(
                                question_id=question_id,
                                model=model,
                                provider=runtime_target.provider,
                                endpoint=runtime_target.base_url,
                                timeout_ms=job.request.timeout_ms,
                                tool_llm_callback=lambda system_prompt, messages, tools, model, provider, endpoint, timeout_ms: call_llm_tool_calling(
                                    server,
                                    system_prompt=system_prompt,
                                    messages=messages,
                                    tools=tools,
                                    target=runtime_target,
                                    model=model,
                                    timeout_ms=timeout_ms,
                                    reasoning_effort=job.request.reasoning_effort,
                                    fallback_state=reasoning_fallback_state,
                                ),
                                abort_signal=job,
                                thinking_mode=thinking_mode,
                                question_timeout_ms=job.request.question_timeout_ms,
                                warmed_models=warmed_models,
                            )
                    finally:
                        REASONING_FALLBACK_STATE.reset(fallback_token)
                    result["reasoning_effort"] = job.request.reasoning_effort
                    result["reasoning_fallback"] = bool(reasoning_fallback_state.get("used"))
                    job.results.append(sql_result_row(job, runtime_target, result))
                    job.progress_completed += 1
                    save_task = asyncio.ensure_future(flush_job_record(server, job))
                    job.track_save(save_task)


async def call_llm_single(
    server: "BenchmarkServer",
    system: str,
    user: str,
    target: BenchmarkTarget,
    model: str,
    timeout_ms: int,
    *,
    reasoning_effort: str = "disabled",
) -> str:
    # Lazy import so the test's ``monkeypatch.setattr(server_module, "_validate_endpoint_url", ...)`` takes effect.
    # SSRF guard for SQL path (speed path validates inside _benchmark_openai/_ollama).
    from python.server import _validate_endpoint_url as _server_validate_endpoint_url
    _server_validate_endpoint_url(target.base_url)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    if target.provider == "openai-compatible":
        headers = {"Content-Type": "application/json"}
        if target.api_key:
            headers["Authorization"] = f"Bearer {target.api_key}"
        # Sampling params intentionally omitted — let the server use whatever
        # is configured in LM Studio / llama.cpp / Ollama. Sending them here
        # would override the user's per-model preset on OpenAI-compatible APIs.
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": 4096,
            "stream": False,
        }
        timeout = httpx.Timeout(
            connect=30.0,
            read=None if timeout_ms <= 0 else timeout_ms / 1000.0,
            write=None if timeout_ms <= 0 else timeout_ms / 1000.0,
            pool=30.0,
        )
        async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
            response = await post_openai_chat_with_reasoning_fallback(
                client,
                f"{target.base_url}{OPENAI_CHAT_PATH}",
                payload,
                reasoning_effort=reasoning_effort,
                model=model,
            )
            if response.status_code >= 400:
                raise RuntimeError(
                    f"HTTP {response.status_code} from {target.base_url}{OPENAI_CHAT_PATH}: {response.text[:300]}"
                )
            data = response.json()
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError("OpenAI-compatible response did not include choices")
        message = choices[0].get("message") or {}
        # Some models (LM Studio, llama.cpp) put thinking in reasoning_content
        content = message.get("content") or message.get("reasoning_content") or ""
        return _coerce_message_content_to_text(content)

    # Ollama: sampling params omitted so the model uses its registered options.
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
    }
    timeout = httpx.Timeout(
        connect=30.0,
        read=None if timeout_ms <= 0 else timeout_ms / 1000.0,
        write=None if timeout_ms <= 0 else timeout_ms / 1000.0,
        pool=30.0,
    )
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(f"{target.base_url}{OLLAMA_CHAT_PATH}", json=payload)
        if response.status_code >= 400:
            raise RuntimeError(f"HTTP {response.status_code} from {target.base_url}{OLLAMA_CHAT_PATH}: {response.text[:300]}")
        data = response.json()
    message = data.get("message") or {}
    return _coerce_message_content_to_text(message.get("content"))


async def call_llm_tool_calling(
    server: "BenchmarkServer",
    system_prompt: str,
    messages: List[Dict[str, Any]],
    tools: List[Dict[str, Any]],
    target: BenchmarkTarget,
    model: str,
    timeout_ms: int,
    *,
    reasoning_effort: str = "disabled",
    fallback_state: Optional[Dict[str, bool]] = None,
) -> Dict[str, Any]:
    """Call LLM with tools (OpenAI-compatible tool-calling endpoint).

    Models that don't support tool_choice (e.g. Gemma, some Ollama models) often
    return HTTP 400/422 with a message containing 'tool' or 'function'. In that
    case we re-try without tools so the tool-calling loop can extract SQL from
    plain text via the fallback branch in run_question_tool_calling.
    """
    # Lazy import so the test's ``monkeypatch.setattr(server_module, "_validate_endpoint_url", ...)`` takes effect.
    # SSRF guard for SQL tool-calling path.
    from python.server import _validate_endpoint_url as _server_validate_endpoint_url
    _server_validate_endpoint_url(target.base_url)
    all_messages = [{"role": "system", "content": system_prompt}] + messages
    headers = {"Content-Type": "application/json"}
    if target.api_key:
        headers["Authorization"] = f"Bearer {target.api_key}"

    # Sampling params omitted — server-side preset wins.
    tool_payload = {
        "model": model,
        "messages": all_messages,
        "tools": tools,
        "tool_choice": "auto",
        "max_tokens": 4096,
        "stream": False,
    }
    timeout = httpx.Timeout(
        connect=30.0,
        read=None if timeout_ms <= 0 else timeout_ms / 1000.0,
        write=None if timeout_ms <= 0 else timeout_ms / 1000.0,
        pool=30.0,
    )

    async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
        response = await post_openai_chat_with_reasoning_fallback(
            client,
            f"{target.base_url}{OPENAI_CHAT_PATH}",
            tool_payload,
            reasoning_effort=reasoning_effort,
            model=model,
            fallback_state=fallback_state,
        )

        # Some models reject tool_choice / tools entirely (400/422/501).
        # Fall back to plain chat so the text-extraction branch can handle it.
        if response.status_code in (400, 422, 501):
            body_text = response.text
            body_lower = body_text.lower()
            # LM Studio: model was unloaded/reloaded mid-request — wait and retry once
            if any(kw in body_lower for kw in ("unloaded", "reloaded")):
                LOG.warning(
                    "Model %s reported unloaded/reloaded — waiting 3s and retrying",
                    model,
                )
                await asyncio.sleep(3)
                response = await post_openai_chat_with_reasoning_fallback(
                    client,
                    f"{target.base_url}{OPENAI_CHAT_PATH}",
                    tool_payload,
                    reasoning_effort=reasoning_effort,
                    model=model,
                    fallback_state=fallback_state,
                )
            elif any(kw in body_lower for kw in ("tool", "function", "not support", "unsupported", "render")):
                LOG.warning(
                    "Model %s at %s rejected tool-calling (%s) — retrying without tools",
                    model, target.base_url, response.status_code,
                )
                plain_payload = {"model": model, "messages": all_messages, "max_tokens": 4096, "stream": False}
                response = await post_openai_chat_with_reasoning_fallback(
                    client,
                    f"{target.base_url}{OPENAI_CHAT_PATH}",
                    plain_payload,
                    reasoning_effort=reasoning_effort,
                    model=model,
                    fallback_state=fallback_state,
                )

        if response.status_code >= 400:
            raise RuntimeError(
                f"HTTP {response.status_code} from {target.base_url}{OPENAI_CHAT_PATH}: {response.text[:400]}"
            )
        data = response.json()

    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"Response from {model} did not include choices: {str(data)[:200]}")
    message = choices[0].get("message", {})

    # reasoning_content is the separate thinking field (LM Studio, llama.cpp)
    content = message.get("content") or message.get("reasoning_content") or ""
    return {
        "content": content,
        "tool_calls": message.get("tool_calls") or [],
        "usage": data.get("usage", {}),
        "model": data.get("model_alias") or data.get("model") or model,
        "reasoning_fallback": bool(fallback_state and fallback_state.get("used")),
    }


def sql_result_row(job: JobState, target: BenchmarkTarget, result: Dict[str, Any]) -> Dict[str, Any]:
    row = dict(result)
    row.setdefault("timestamp", ts_utc())
    row["job_id"] = job.job_id
    row["benchmark_type"] = "sql"
    row["provider_id"] = target.provider_id
    row["provider_label"] = target.provider_label
    row["provider_type"] = target.provider
    row["provider"] = target.provider
    row["endpoint"] = target.base_url
    row["model"] = result.get("model") or target.models[0]
    row["thinking_mode"] = result.get("thinking_mode", "off")
    row["reasoning_effort"] = result.get("reasoning_effort", job.request.reasoning_effort)
    row["reasoning_fallback"] = bool(result.get("reasoning_fallback", False))
    return row


async def run_sequential(server: "BenchmarkServer", job: JobState, target: BenchmarkTarget) -> None:
    from python.persistence import flush_job_record
    job.current_provider_id = target.provider_id
    job.current_provider_label = target.provider_label
    for model in target.models:
        if job.stop_requested:
            return
        job.current_model = model
        for _ in range(job.request.warmup_runs):
            if job.stop_requested:
                return
            await job.set_phase("warming_up", f"Warming up {model}", model=model, run_index=0, benchmark_type="speed")
            await run_single_benchmark(server, job, target, model, 0)
        for run_index in range(1, job.request.repeat_count + 1):
            if job.stop_requested:
                return
            await job.set_phase("running_request", f"Running speed test for {model} (run {run_index}/{job.request.repeat_count})", model=model, run_index=run_index, benchmark_type="speed")
            result = await run_single_benchmark(server, job, target, model, run_index)
            job.results.append(result)
            job.progress_completed += 1
            # Incremental save: a server crash mid-run no longer drops all
            # already-finished results. Mirrors the SQL path.
            save_task = asyncio.ensure_future(flush_job_record(server, job))
            job.track_save(save_task)
            await job.set_phase("result_recorded", f"Recorded speed result for {model} (run {run_index}/{job.request.repeat_count})", model=model, run_index=run_index, benchmark_type="speed")


async def run_parallel(server: "BenchmarkServer", job: JobState, target: BenchmarkTarget) -> None:
    from python.persistence import flush_job_record
    job.current_provider_id = target.provider_id
    job.current_provider_label = target.provider_label
    prompt_hash = hashlib.sha256(job.request.prompt.encode("utf-8")).hexdigest()
    semaphore = asyncio.Semaphore(job.request.concurrency)

    async def worker(model: str, run_index: int) -> Dict[str, Any]:
        async with semaphore:
            if job.stop_requested:
                return stopped_result(job, target, model, run_index, prompt_hash)
            job.current_model = model
            job.current_provider_id = target.provider_id
            job.current_provider_label = target.provider_label
            await job.set_phase(
                "warming_up" if run_index == 0 else "running_request",
                f"{'Warming up' if run_index == 0 else 'Running speed test for'} {model}"
                + ("" if run_index == 0 else f" (run {run_index}/{job.request.repeat_count})"),
                model=model,
                run_index=run_index,
                benchmark_type="speed",
            )
            try:
                return await run_single_benchmark(server, job, target, model, run_index)
            except asyncio.CancelledError:
                return stopped_result(job, target, model, run_index, prompt_hash)

    tasks = [
        asyncio.create_task(worker(model, run_index))
        for model in target.models
        for _ in range(job.request.warmup_runs)
        for run_index in [0]
    ] + [
        asyncio.create_task(worker(model, run_index))
        for model in target.models
        for run_index in range(1, job.request.repeat_count + 1)
    ]
    for task in asyncio.as_completed(tasks):
        result = await task
        if result.get("success") == "stopped":
            continue
        if result.get("run_index") == 0:
            continue
        job.results.append(result)
        job.progress_completed += 1
        # Incremental save (parallel path).
        save_task = asyncio.ensure_future(flush_job_record(server, job))
        job.track_save(save_task)
        await job.set_phase("result_recorded", f"Recorded speed result for {result.get('model', 'model')}", model=result.get("model"), run_index=result.get("run_index"), benchmark_type="speed")
        if job.stop_requested:
            for pending in tasks:
                if not pending.done():
                    pending.cancel()
            return


async def run_single_benchmark(
    server: "BenchmarkServer",
    job: JobState,
    target: BenchmarkTarget,
    model: str,
    run_index: int,
) -> Dict[str, Any]:
    prompt_hash = hashlib.sha256(job.request.prompt.encode("utf-8")).hexdigest()
    started = time.perf_counter()
    start_stamp = ts_utc()
    try:
        if target.provider == "openai-compatible":
            metrics = await server._benchmark_openai(job.request, target, model, job=job, run_index=run_index)
        else:
            metrics = await server._benchmark_ollama(job.request, target, model, job=job, run_index=run_index)
        await job.set_phase("scoring", f"Scoring speed result for {model}", model=model, run_index=run_index, benchmark_type="speed")
        latency_ms = round((time.perf_counter() - started) * 1000.0, 2)
        return _build_speed_row(
            job, target, model, run_index,
            prompt_hash=prompt_hash,
            timestamp=start_stamp,
            metrics=metrics,
            success=True,
            error="",
            latency_ms=latency_ms,
        )
    except (httpx.HTTPError, ValueError, RuntimeError, asyncio.TimeoutError) as exc:
        latency_ms = round((time.perf_counter() - started) * 1000.0, 2)
        return _build_speed_row(
            job, target, model, run_index,
            prompt_hash=prompt_hash,
            timestamp=start_stamp,
            metrics=None,
            success=False,
            error=str(exc),
            latency_ms=latency_ms,
        )


def stopped_result(
    job: JobState,
    target: BenchmarkTarget,
    model: str,
    run_index: int,
    prompt_hash: str,
) -> Dict[str, Any]:
    return _build_speed_row(
        job, target, model, run_index,
        prompt_hash=prompt_hash,
        timestamp=ts_utc(),
        metrics=None,
        success="stopped",
        error="stopped",
        latency_ms=None,
    )


async def benchmark_openai(
    server: "BenchmarkServer",
    spec,
    target: BenchmarkTarget,
    model: str,
    *,
    job: Optional[JobState] = None,
    run_index: Optional[int] = None,
) -> Dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if target.api_key:
        headers["Authorization"] = f"Bearer {target.api_key}"
    # Sampling params (temperature/top_p/penalties) intentionally omitted so
    # the LLM server uses whatever its own model preset specifies. Sending
    # values here would silently override the user's per-model config in
    # LM Studio / llama.cpp.
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": spec.prompt}],
        "max_tokens": spec.max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    timeout = httpx.Timeout(spec.timeout_ms / 1000.0)
    start = time.perf_counter()
    first_token_at: Optional[float] = None
    completion_tokens: Optional[int] = None
    prompt_tokens: Optional[int] = None
    if job is not None:
        await job.set_phase("waiting_first_token", f"Waiting for first token from {model}", model=model, run_index=run_index, benchmark_type="speed")
    # Lazy import so the test's ``monkeypatch.setattr(server_module, "_validate_endpoint_url", ...)`` takes effect
    from python.server import _validate_endpoint_url as _server_validate_endpoint_url
    _server_validate_endpoint_url(target.base_url)
    async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
        async with client.stream("POST", f"{target.base_url}{OPENAI_CHAT_PATH}", json=payload) as response:
            if response.status_code >= 400:
                body = (await response.aread()).decode("utf-8", errors="replace")
                raise RuntimeError(f"HTTP {response.status_code} from {target.base_url}{OPENAI_CHAT_PATH}: {body[:300]}")
            async for line in response.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    break
                data = json.loads(data_str)
                if data.get("error"):
                    err = data["error"]
                    msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                    raise RuntimeError(f"Server error in stream: {msg}")
                choices = data.get("choices") or []
                if choices:
                    choice_err = choices[0].get("error") or choices[0].get("finish_reason") == "error"
                    if choice_err:
                        raise RuntimeError(f"Server error in choice: {choices[0].get('error') or 'unknown'}")
                    delta = choices[0].get("delta") or {}
                    content = delta.get("content")
                    reasoning = delta.get("reasoning_content")
                    if first_token_at is None and (content is not None or reasoning is not None):
                        first_token_at = time.perf_counter()
                        if job is not None:
                            await job.set_phase("streaming", f"Streaming response from {model}", model=model, run_index=run_index, benchmark_type="speed")
                usage = data.get("usage") or {}
                if usage:
                    prompt_tokens = usage.get("prompt_tokens", prompt_tokens)
                    completion_tokens = usage.get("completion_tokens", completion_tokens)
    # Counting stream chunks is NOT a tokens fallback: one chunk can hold many
    # tokens, so deriving decode_tps from chunk count under-reports by N×. If
    # the server doesn't include usage, leave completion_tokens/decode_tps as
    # None — the UI shows "n/a" rather than a misleading number.
    end = time.perf_counter()
    latency_ms = (end - start) * 1000.0
    ttft_ms = ((first_token_at or end) - start) * 1000.0 if first_token_at else None
    decode_tps = None
    prefill_tps = None
    completion_tokens_capped = None
    decode_tokens_measured = None
    if first_token_at and completion_tokens is not None:
        decode_seconds = max(end - first_token_at, 1e-6)
        decode_tokens_measured = completion_tokens
        completion_tokens_capped = min(completion_tokens, spec.max_tokens)
        decode_tps = round(decode_tokens_measured / decode_seconds, 2)
    return {
        "latency_ms": round(latency_ms, 2),
        "total_time_ms": round(latency_ms, 2),
        "ttft_ms": round(ttft_ms, 2) if ttft_ms is not None else None,
        "prefill_tps": prefill_tps,
        "decode_tps": decode_tps,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "completion_tokens_capped": completion_tokens_capped,
        "decode_tokens_measured": decode_tokens_measured,
    }


async def benchmark_ollama(
    server: "BenchmarkServer",
    spec,
    target: BenchmarkTarget,
    model: str,
    *,
    job: Optional[JobState] = None,
    run_index: Optional[int] = None,
) -> Dict[str, Any]:
    # Sampling params omitted — Ollama uses the model's Modelfile defaults.
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": spec.prompt}],
        "options": {
            "num_predict": spec.max_tokens,
        },
        "stream": False,
    }
    timeout = httpx.Timeout(spec.timeout_ms / 1000.0)
    if job is not None:
        await job.set_phase("waiting_response", f"Waiting for Ollama response from {model}", model=model, run_index=run_index, benchmark_type="speed")
    # Lazy import so the test's ``monkeypatch.setattr(server_module, "_validate_endpoint_url", ...)`` takes effect
    from python.server import _validate_endpoint_url as _server_validate_endpoint_url
    _server_validate_endpoint_url(target.base_url)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(f"{target.base_url}{OLLAMA_CHAT_PATH}", json=payload)
        if response.status_code >= 400:
            raise RuntimeError(f"HTTP {response.status_code} from {target.base_url}{OLLAMA_CHAT_PATH}: {response.text[:300]}")
        data = response.json()
    prompt_tokens = data.get("prompt_eval_count")
    completion_tokens = data.get("eval_count")
    prompt_eval_duration = data.get("prompt_eval_duration")
    eval_duration = data.get("eval_duration")
    ttft_ms = round((prompt_eval_duration or 0) / 1_000_000.0, 2) if prompt_eval_duration else None
    total_time_ms = None
    if prompt_eval_duration or eval_duration:
        total_time_ms = round(((prompt_eval_duration or 0) + (eval_duration or 0)) / 1_000_000.0, 2)
    prefill_tps = None
    decode_tps = None
    completion_tokens_capped = None
    decode_tokens_measured = None
    if prompt_tokens and prompt_eval_duration:
        prefill_tps = round(prompt_tokens / max(prompt_eval_duration / 1_000_000_000.0, 1e-6), 2)
    if completion_tokens is not None and eval_duration:
        decode_tokens_measured = completion_tokens
        completion_tokens_capped = min(completion_tokens, spec.max_tokens)
        decode_tps = round(completion_tokens / max(eval_duration / 1_000_000_000.0, 1e-6), 2)
    return {
        "latency_ms": total_time_ms,
        "total_time_ms": total_time_ms,
        "ttft_ms": ttft_ms,
        "prefill_tps": prefill_tps,
        "decode_tps": decode_tps,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "completion_tokens_capped": completion_tokens_capped,
        "decode_tokens_measured": decode_tokens_measured,
    }


def build_report(job: JobState) -> str:
    if job.request.benchmark_type == "sql":
        return build_sql_report(job)
    return build_speed_report(job)


def build_speed_report(job: JobState) -> str:
    lines = [
        f"Job: {job.job_id}",
        f"Status: {job.status}",
        f"Benchmark type: {job.request.benchmark_type}",
        f"Endpoint: {job.request.base_url}",
        f"Mode: {job.request.mode}",
        f"Models: {', '.join(job.request.models)}",
        f"Completed: {job.progress_completed}/{job.progress_total}",
    ]
    successful = [item for item in job.results if item.get("success") is True]
    failed = [item for item in job.results if item.get("success") is False]
    lines.append(f"Successful runs: {len(successful)}")
    lines.append(f"Failed runs: {len(failed)}")
    if successful:
        best_latency = min(item["latency_ms"] for item in successful if item.get("latency_ms") is not None)
        lines.append(f"Best latency: {best_latency} ms")
        decode_values = [item["decode_tps"] for item in successful if item.get("decode_tps") is not None]
        if decode_values:
            lines.append(f"Best decode speed: {max(decode_values)} tok/s")
    if job.errors:
        lines.append("Errors:")
        lines.extend(f"- {error}" for error in job.errors)
    return "\n".join(lines)


def build_sql_report(job: JobState) -> str:
    total = len(job.results)
    successful = [item for item in job.results if item.get("success") is True]
    failed = [item for item in job.results if item.get("success") is False]
    lines = [
        f"Job: {job.job_id}",
        f"Status: {job.status}",
        f"Benchmark type: {job.request.benchmark_type}",
        f"Endpoint: {job.request.base_url}",
        f"Model: {job.request.models[0] if job.request.models else ''}",
        f"Completed: {job.progress_completed}/{job.progress_total}",
        f"Questions passed: {len(successful)}/{total}",
        f"Questions failed: {len(failed)}",
    ]
    if failed:
        lines.append("Failed questions:")
        for item in failed:
            lines.append(f"- q{item.get('question_id')}: {item.get('error', '')}")
    if job.errors:
        lines.append("Errors:")
        lines.extend(f"- {error}" for error in job.errors)
    return "\n".join(lines)
