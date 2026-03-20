# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Christian Meurer — https://github.com/christianmeurer/Lula
from __future__ import annotations

import asyncio
import concurrent.futures
import os
import time
from typing import Any, cast

from lg_orch.logging import get_logger
from lg_orch.remote_api import push_run_event
from lg_orch.tools import InferenceClient
from lg_orch.tools.inference_client import InferenceResponse
from lg_orch.trace import append_event

_SYSTEM_PROMPT = (
    "You are a coder for a repo-aware coding assistant. "
    "Given the handoff objective, file scope, evidence, and constraints, "
    "produce a concise patch description or implementation guidance. "
    "Be specific: name the exact files, functions, and minimal changes required. "
    "Prefer the smallest correct diff."
)

_MAX_EVIDENCE_CHARS = 800
_MAX_CONSTRAINTS_CHARS = 400


def _coerce_handoff(raw: object) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None

    producer = str(raw.get("producer", "")).strip()
    consumer = str(raw.get("consumer", "")).strip()
    objective = str(raw.get("objective", "")).strip()
    if not producer or not consumer or not objective:
        return None

    file_scope_raw = raw.get("file_scope", [])
    file_scope = [
        str(path).strip()
        for path in file_scope_raw
        if isinstance(file_scope_raw, list) and str(path).strip()
    ]
    evidence_raw = raw.get("evidence", [])
    evidence = [dict(entry) for entry in evidence_raw if isinstance(entry, dict)] if isinstance(evidence_raw, list) else []
    constraints_raw = raw.get("constraints", [])
    constraints = [
        str(item).strip() for item in constraints_raw if isinstance(item, str) and item.strip()
    ] if isinstance(constraints_raw, list) else []
    acceptance_checks_raw = raw.get("acceptance_checks", [])
    acceptance_checks = [
        str(item).strip()
        for item in acceptance_checks_raw
        if isinstance(item, str) and item.strip()
    ] if isinstance(acceptance_checks_raw, list) else []
    provenance_raw = raw.get("provenance", [])
    provenance = [
        str(item).strip() for item in provenance_raw if isinstance(item, str) and item.strip()
    ] if isinstance(provenance_raw, list) else []
    retry_budget_raw = raw.get("retry_budget", 1)
    retry_budget = retry_budget_raw if isinstance(retry_budget_raw, int) and retry_budget_raw >= 0 else 1

    return {
        "producer": producer,
        "consumer": consumer,
        "objective": objective,
        "file_scope": file_scope,
        "evidence": evidence,
        "constraints": constraints,
        "acceptance_checks": acceptance_checks,
        "retry_budget": retry_budget,
        "provenance": provenance,
    }


def _first_step(plan: dict[str, Any]) -> dict[str, Any] | None:
    steps_raw = plan.get("steps", [])
    if not isinstance(steps_raw, list):
        return None
    for step in steps_raw:
        if isinstance(step, dict):
            return step
    return None


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        normalized = item.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
    return out


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
        log.info("coder_no_provider", provider=provider)
        return None
    model = str(slot.get("model", "")).strip()
    if not model:
        log.info("coder_no_model")
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
            log.info("coder_no_api_key", provider=provider)
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
        if not api_key:
            log.info("coder_no_api_key", provider=provider)
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


def _llm_code_synthesis(state: dict[str, Any], *, handoff: dict[str, Any]) -> str | None:
    """Call the LLM to produce patch guidance, streaming chunks as SSE events.

    Returns the synthesized text or None when the model is not configured.
    Falls back to ``chat_completion`` (non-streaming) when ``run_id`` is absent
    or streaming raises.
    """
    cfg = _get_inference_config(state)
    if cfg is None:
        return None
    model, api_key, base_url, timeout_s = cfg

    objective = str(handoff.get("objective", "")).strip()
    file_scope = handoff.get("file_scope", [])
    file_scope_str = ", ".join(str(p) for p in file_scope if p) if isinstance(file_scope, list) else ""
    evidence_raw = handoff.get("evidence", [])
    evidence_parts: list[str] = []
    if isinstance(evidence_raw, list):
        for entry in evidence_raw:
            if isinstance(entry, dict):
                detail = str(entry.get("detail", "")).strip()
                if detail:
                    evidence_parts.append(detail)
    evidence_str = "\n".join(evidence_parts)[:_MAX_EVIDENCE_CHARS]
    constraints_raw = handoff.get("constraints", [])
    constraints_str = "\n".join(
        str(c) for c in constraints_raw if isinstance(c, str) and c.strip()
    )[:_MAX_CONSTRAINTS_CHARS]

    user_prompt = (
        f"Objective: {objective}\n\n"
        f"File scope: {file_scope_str or '(not specified)'}\n\n"
        f"Evidence:\n{evidence_str or '(none)'}\n\n"
        f"Constraints:\n{constraints_str or '(none)'}\n\n"
        "Produce concise patch guidance."
    )

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
                    temperature=0.2,
                    max_tokens=800,
                    run_id=run_id,
                    node="coder",
                )
            except Exception:
                response = client.chat_completion(
                    model=model,
                    system_prompt=_SYSTEM_PROMPT,
                    user_prompt=user_prompt,
                    temperature=0.2,
                    max_tokens=800,
                )
        else:
            response = client.chat_completion(
                model=model,
                system_prompt=_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                temperature=0.2,
                max_tokens=800,
            )
    finally:
        client.close()

    return response if isinstance(response, str) else response.text


def coder(state: dict[str, Any]) -> dict[str, Any]:
    log = get_logger()
    state = append_event(state, kind="node", data={"name": "coder", "phase": "start"})

    plan_raw = state.get("plan", {})
    plan = dict(plan_raw) if isinstance(plan_raw, dict) else {}
    step = _first_step(plan)
    if step is None:
        return append_event(state, kind="node", data={"name": "coder", "phase": "end", "handoff": "none"})

    active_handoff = _coerce_handoff(state.get("active_handoff"))
    step_handoff = _coerce_handoff(step.get("handoff"))
    source_handoff = active_handoff if active_handoff and active_handoff.get("consumer") == "coder" else step_handoff

    if source_handoff is None or source_handoff.get("consumer") != "coder":
        return append_event(state, kind="node", data={"name": "coder", "phase": "end", "handoff": "pass_through"})

    step_id = str(step.get("id", "step")).strip() or "step"
    step_description = str(step.get("description", "")).strip()
    expected_outcome = str(step.get("expected_outcome", "")).strip()
    files_touched_raw = step.get("files_touched", [])
    files_touched = [
        str(path).strip() for path in files_touched_raw if isinstance(files_touched_raw, list) and str(path).strip()
    ]

    evidence = list(source_handoff.get("evidence", []))
    if step_description:
        evidence.append({"kind": "plan_step", "ref": step_id, "detail": step_description})
    if expected_outcome:
        evidence.append({"kind": "expected_outcome", "ref": step_id, "detail": expected_outcome})

    constraints = list(source_handoff.get("constraints", []))
    constraints.append("Execute only the current step's planned tools.")

    acceptance_checks = list(source_handoff.get("acceptance_checks", []))
    if expected_outcome and expected_outcome not in acceptance_checks:
        acceptance_checks.append(expected_outcome)

    next_handoff = {
        "producer": "coder",
        "consumer": "executor",
        "objective": "Execute the bounded tool sequence prepared by the coder for the current step.",
        "file_scope": _dedupe(list(source_handoff.get("file_scope", [])) + files_touched),
        "evidence": evidence,
        "constraints": _dedupe(constraints),
        "acceptance_checks": _dedupe(acceptance_checks),
        "retry_budget": int(source_handoff.get("retry_budget", 1) or 0),
        "provenance": _dedupe(list(source_handoff.get("provenance", [])) + [f"coder:{step_id}"]),
    }

    # Optional LLM synthesis: generate patch guidance when a model is configured.
    llm_guidance: str | None = None
    try:
        llm_guidance = _llm_code_synthesis(state, handoff=source_handoff)
    except Exception as exc:
        log.warning("coder_llm_synthesis_failed", error=str(exc))

    if llm_guidance:
        _evidence: list[Any] = list(cast(list[Any], next_handoff["evidence"]))
        next_handoff["evidence"] = _evidence + [
            {"kind": "llm_guidance", "ref": step_id, "detail": llm_guidance[:600]}
        ]

    log.info("coder_handoff_prepared", step_id=step_id, file_scope=next_handoff["file_scope"])
    out = {**state, "active_handoff": next_handoff, "retry_target": None}
    return append_event(
        out,
        kind="node",
        data={"name": "coder", "phase": "end", "handoff_consumer": "executor", "step_id": step_id},
    )
