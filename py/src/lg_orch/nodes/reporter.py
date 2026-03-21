# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Christian Meurer — https://github.com/christianmeurer/Lula
from __future__ import annotations

import asyncio
import concurrent.futures
import os
import time
from typing import Any

from lg_orch.logging import get_logger
from lg_orch.remote_api import push_run_event
from lg_orch.tools import InferenceClient
from lg_orch.tools.inference_client import InferenceResponse
from lg_orch.trace import append_event

_SYSTEM_PROMPT = (
    "You are a reporter for a repo-aware coding assistant. "
    "Given the user's request and the tool results gathered, produce a direct, "
    "complete, human-readable answer. Be specific: include file names, line numbers, "
    "function names, and code snippets where relevant. If the request was a code "
    "change, confirm what was changed and whether tests passed."
)

_MAX_STDOUT_PER_RESULT = 500
_MAX_STDERR_PER_RESULT = 200
_MAX_SUMMARY_CHARS = 2000


def _summarize_tool_results(tool_results: list[Any]) -> str:
    parts: list[str] = []
    for tr in tool_results:
        if not isinstance(tr, dict):
            continue
        tool = str(tr.get("tool", "unknown"))
        stdout = str(tr.get("stdout", ""))[:_MAX_STDOUT_PER_RESULT]
        stderr = str(tr.get("stderr", ""))[:_MAX_STDERR_PER_RESULT]
        ok = tr.get("ok", True)
        chunk = f"[{tool}] ok={ok}"
        if stdout:
            chunk += f"\nstdout: {stdout}"
        if stderr:
            chunk += f"\nstderr: {stderr}"
        parts.append(chunk)
    summary = "\n\n".join(parts)
    return summary[:_MAX_SUMMARY_CHARS]


def _structured_summary(state: dict[str, Any]) -> str:
    repo_context = state.get("repo_context", {})
    tool_results = state.get("tool_results", [])
    verification_raw = state.get("verification", {})
    verification = dict(verification_raw) if isinstance(verification_raw, dict) else {}
    lines: list[str] = []
    lines.append(f"intent: {state.get('intent')}")
    lines.append(f"repo_root: {repo_context.get('repo_root')}")
    lines.append(f"top_level: {repo_context.get('top_level')}")
    if tool_results:
        lines.append(f"tool_calls: {len(tool_results)}")
    if "ok" in verification:
        lines.append(f"verification_ok: {verification.get('ok')}")
    if "acceptance_ok" in verification:
        lines.append(f"acceptance_ok: {verification.get('acceptance_ok')}")
    acceptance_checks_raw = verification.get("acceptance_checks", [])
    acceptance_checks = (
        [entry for entry in acceptance_checks_raw if isinstance(entry, dict)]
        if isinstance(acceptance_checks_raw, list)
        else []
    )
    unmet = [
        str(entry.get("criterion", "")).strip()
        for entry in acceptance_checks
        if bool(entry.get("ok", False)) is False and str(entry.get("criterion", "")).strip()
    ]
    if unmet:
        lines.append(f"acceptance_unmet: {unmet}")
    halt_reason = str(state.get("halt_reason", "")).strip()
    if halt_reason:
        lines.append(f"halt_reason: {halt_reason}")
    return "\n".join(lines)


def _get_inference_config(
    state: dict[str, Any],
) -> tuple[str, str, str, int] | None:
    """Return (model, api_key, base_url, timeout_s) or None if not configured."""
    log = get_logger()
    models_raw = state.get("_models", {})
    models = models_raw if isinstance(models_raw, dict) else {}
    slot_raw = models.get("planner", {})
    slot = slot_raw if isinstance(slot_raw, dict) else {}
    provider = str(slot.get("provider", "local")).strip().lower()
    if provider in {"", "local"}:
        log.info("reporter_no_provider", provider=provider)
        return None
    model = str(slot.get("model", "")).strip()
    if not model:
        log.info("reporter_no_model")
        return None

    runtime_raw = state.get("_model_provider_runtime", {})
    runtime = runtime_raw if isinstance(runtime_raw, dict) else {}

    if provider == "openai_compatible":
        oc_raw = runtime.get("openai_compatible", {})
        oc_cfg = oc_raw if isinstance(oc_raw, dict) else {}
        api_key = str(oc_cfg.get("api_key") or "").strip()
        if not api_key:
            api_key = (os.environ.get("OPENAI_API_KEY") or "").strip()
        if not api_key:
            log.info("reporter_no_api_key", provider=provider)
            return None
        base_url = str(oc_cfg.get("base_url", "https://api.openai.com/v1")).strip().rstrip("/")
        timeout_raw = oc_cfg.get("timeout_s", 60)
    else:
        do_raw = runtime.get("digitalocean", {})
        do_cfg = do_raw if isinstance(do_raw, dict) else {}
        api_key = str(do_cfg.get("api_key") or "").strip()
        if not api_key:
            api_key = (
                os.environ.get("MODEL_ACCESS_KEY")
                or os.environ.get("DIGITAL_OCEAN_MODEL_ACCESS_KEY")
                or ""
            ).strip()
        log.info(
            "reporter_api_key_check",
            provider=provider,
            has_key=bool(api_key),
            key_len=len(api_key),
        )
        if not api_key:
            log.info("reporter_no_api_key", provider=provider)
            return None
        base_url = str(do_cfg.get("base_url", "https://inference.do-ai.run/v1")).strip().rstrip("/")
        timeout_raw = do_cfg.get("timeout_s", 60)

    if not base_url or not (base_url.startswith("http://") or base_url.startswith("https://")):
        return None
    timeout_s = int(timeout_raw) if isinstance(timeout_raw, int) and timeout_raw > 0 else 60
    return (model, api_key, base_url, timeout_s)


def _stream_llm_with_events(
    client: InferenceClient,
    *,
    model: str,
    system_prompt: str,
    user_prompt: str,
    temperature: float,
    max_tokens: int,
    run_id: str,
    node: str,
) -> InferenceResponse:
    """Run streaming LLM call, emitting llm_chunk SSE events per token.

    Runs the async generator in a thread pool to remain safe in sync graph nodes
    that may already have a running event loop (e.g. LangGraph internals).
    """
    started = time.perf_counter()
    chunks: list[str] = []

    async def _run() -> str:
        async for token in client.chat_completion_stream(
            model=model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
        ):
            chunks.append(token)
            push_run_event(run_id, {"type": "llm_chunk", "node": node, "delta": token})
        return "".join(chunks)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(asyncio.run, _run())
        text = future.result()

    latency_ms = int((time.perf_counter() - started) * 1000)
    return InferenceResponse(
        text=text,
        latency_ms=latency_ms,
        provider="",
        model=model,
        usage=None,
        cache_metadata=None,
        headers=None,
    )


def _llm_synthesis(state: dict[str, Any]) -> str | None:
    cfg = _get_inference_config(state)
    if cfg is None:
        return None
    model, api_key, base_url, timeout_s = cfg

    request = str(state.get("request", "")).strip()
    tool_results_raw = state.get("tool_results", [])
    tool_results = tool_results_raw if isinstance(tool_results_raw, list) else []
    summarized = _summarize_tool_results(tool_results)

    user_prompt = f"Request: {request}\n\nTool results:\n{summarized}\n\nProduce the final answer."

    run_id_raw = state.get("run_id")
    run_id = str(run_id_raw).strip() if isinstance(run_id_raw, str) and run_id_raw.strip() else None

    client = InferenceClient(base_url=base_url, api_key=api_key, timeout_s=timeout_s)
    try:
        if run_id is not None:
            try:
                response = _stream_llm_with_events(
                    client,
                    model=model,
                    system_prompt=_SYSTEM_PROMPT,
                    user_prompt=user_prompt,
                    temperature=0.3,
                    max_tokens=800,
                    run_id=run_id,
                    node="reporter",
                )
            except Exception:
                response = client.chat_completion(
                    model=model,
                    system_prompt=_SYSTEM_PROMPT,
                    user_prompt=user_prompt,
                    temperature=0.3,
                    max_tokens=800,
                )
        else:
            response = client.chat_completion(
                model=model,
                system_prompt=_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                temperature=0.3,
                max_tokens=800,
            )
    finally:
        client.close()

    return response if isinstance(response, str) else response.text


def reporter(state: dict[str, Any]) -> dict[str, Any]:
    log = get_logger()
    state = append_event(state, kind="node", data={"name": "reporter", "phase": "start"})
    try:
        final: str | None = None
        llm_error: str = ""
        try:
            final = _llm_synthesis(state)
        except Exception as llm_exc:
            log.warning("reporter_llm_failed", error=str(llm_exc))
            llm_error = f"llm_error: {type(llm_exc).__name__}: {llm_exc}"
            final = None

        if not final:
            summary = _structured_summary(state)
            final = f"{summary}\n{llm_error}" if llm_error else summary
    except Exception as exc:
        log.error("reporter_failed", error=str(exc))
        final = f"error: reporter failed: {exc}"

    out = {**state, "final": final}
    return append_event(out, kind="node", data={"name": "reporter", "phase": "end"})
