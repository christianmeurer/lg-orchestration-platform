from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from lg_orch.logging import get_logger
from lg_orch.memory import approx_token_count
from lg_orch.model_routing import latest_model_route, record_inference_telemetry, record_model_route
from lg_orch.state import RouterDecision
from lg_orch.tools import InferenceClient
from lg_orch.trace import append_event

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.DOTALL | re.IGNORECASE)
_WORD_RE = re.compile(r"[a-z0-9']+")


def _extract_json_block(raw: str) -> str:
    fenced = _JSON_FENCE_RE.search(raw)
    if fenced is not None:
        return fenced.group(1).strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1 and end >= start:
        return raw[start : end + 1].strip()
    return raw.strip()


def _classify_intent(request: str) -> str:
    r = request.lower()
    words = set(_WORD_RE.findall(r))
    if words.intersection({"implement", "add", "change", "fix", "refactor"}):
        return "code_change"
    if words.intersection({"debug", "error", "panic", "exception"}) or "stack trace" in r:
        return "debug"
    if words.intersection({"research", "compare", "survey", "latest"}):
        return "research"
    if "why" in words or "how" in words or "explain" in words:
        return "question"
    return "analysis"


def _default_route(state: dict[str, Any]) -> RouterDecision:
    request = str(state.get("request", "")).strip()
    intent = _classify_intent(request)
    verification_raw = state.get("verification", {})
    verification = dict(verification_raw) if isinstance(verification_raw, dict) else {}
    recovery_raw = verification.get("recovery", {})
    recovery = dict(recovery_raw) if isinstance(recovery_raw, dict) else {}
    recovery_packet_raw = verification.get("recovery_packet", state.get("recovery_packet", {}))
    recovery_packet = dict(recovery_packet_raw) if isinstance(recovery_packet_raw, dict) else {}
    if recovery_packet:
        recovery = recovery_packet

    routing_raw = state.get("_model_routing_policy", {})
    routing = dict(routing_raw) if isinstance(routing_raw, dict) else {}
    interactive_limit = int(routing.get("interactive_context_limit", 1800) or 1800)
    default_cache_affinity = str(routing.get("default_cache_affinity", "workspace")).strip() or "workspace"

    repo_context_raw = state.get("repo_context", {})
    repo_context = dict(repo_context_raw) if isinstance(repo_context_raw, dict) else {}
    planner_context_raw = repo_context.get("planner_context", {})
    planner_context = dict(planner_context_raw) if isinstance(planner_context_raw, dict) else {}
    token_estimate_raw = planner_context.get("token_estimate", 0)
    token_estimate = int(token_estimate_raw) if isinstance(token_estimate_raw, int) else 0
    if token_estimate <= 0:
        token_estimate = approx_token_count(str(repo_context.get("repo_map", "")))
    working_set_raw = repo_context.get("working_set", {})
    working_set = dict(working_set_raw) if isinstance(working_set_raw, dict) else {}
    working_set_tokens_raw = planner_context.get(
        "working_set_token_estimate",
        working_set.get("token_estimate", 0),
    )
    working_set_tokens = (
        int(working_set_tokens_raw) if isinstance(working_set_tokens_raw, int) else token_estimate
    )
    compression_raw = repo_context.get("compression", {})
    compression = dict(compression_raw) if isinstance(compression_raw, dict) else {}
    pressure_raw = compression.get("pressure", {})
    pressure = dict(pressure_raw) if isinstance(pressure_raw, dict) else {}
    overall_pressure_raw = pressure.get("overall", {})
    overall_pressure = dict(overall_pressure_raw) if isinstance(overall_pressure_raw, dict) else {}
    compression_score_raw = planner_context.get("compression_pressure", overall_pressure.get("score", 0))
    compression_score = int(compression_score_raw) if isinstance(compression_score_raw, int) else 0
    facts_raw = state.get("facts", [])
    state_fact_count = len(facts_raw) if isinstance(facts_raw, list) else 0
    fact_count_raw = planner_context.get("fact_count", state_fact_count)
    fact_count = int(fact_count_raw) if isinstance(fact_count_raw, int) else state_fact_count
    semantic_memories_raw = repo_context.get("semantic_memories", [])
    semantic_memories = (
        [entry for entry in semantic_memories_raw if isinstance(entry, dict)]
        if isinstance(semantic_memories_raw, list)
        else []
    )
    semantic_memory_count_raw = planner_context.get("semantic_memory_count", len(semantic_memories))
    semantic_memory_count = (
        int(semantic_memory_count_raw)
        if isinstance(semantic_memory_count_raw, int)
        else len(semantic_memories)
    )

    retry_target = str(state.get("retry_target", "")).strip()
    failure_fingerprint = str(verification.get("failure_fingerprint", "")).strip()
    current_loop_raw = state.get("budgets", {})
    current_loop_state = dict(current_loop_raw) if isinstance(current_loop_raw, dict) else {}
    current_loop = int(current_loop_state.get("current_loop", 0) or 0)

    if retry_target == "router" or recovery:
        failure_class = str(recovery.get("failure_class", verification.get("failure_class", "verification_failed")))
        context_scope = str(recovery.get("context_scope", "working_set")) or "working_set"
        prefix_segment = "recovery_working_set" if context_scope != "stable_prefix" else "stable_prefix"
        return RouterDecision(
            intent=intent,  # type: ignore[arg-type]
            task_class=failure_class or "recovery",
            lane="recovery",
            rationale="verification requested a recovery route"
            if compression_score <= 0
            else "verification requested recovery and compression pressure favors a stronger lane",
            context_scope=context_scope,  # type: ignore[arg-type]
            latency_sensitive=False,
            cache_affinity=f"{default_cache_affinity}:recovery:{failure_fingerprint or current_loop}",
            prefix_segment=prefix_segment,
            context_tokens=max(token_estimate, working_set_tokens),
            compression_pressure=compression_score,
            fact_count=fact_count,
        )

    if (
        intent in {"code_change", "refactor", "debug"}
        or token_estimate > interactive_limit
        or working_set_tokens > interactive_limit
        or compression_score > 0
        or fact_count >= 3
        or semantic_memory_count >= 2
    ):
        rationale = "request complexity or context size requires deeper planning"
        if compression_score > 0:
            rationale = "context compression pressure requires deeper planning"
        elif fact_count >= 3:
            rationale = "recovery memory indicates deeper planning is needed"
        elif semantic_memory_count >= 2:
            rationale = "semantic memory recall indicates deeper planning is needed"
        return RouterDecision(
            intent=intent,  # type: ignore[arg-type]
            task_class="deep_planning",
            lane="deep_planning",
            rationale=rationale,
            context_scope="stable_prefix",
            latency_sensitive=False,
            cache_affinity=f"{default_cache_affinity}:planner:{current_loop}:c{compression_score}",
            prefix_segment="stable_prefix",
            context_tokens=max(token_estimate, working_set_tokens),
            compression_pressure=compression_score,
            fact_count=max(fact_count, semantic_memory_count),
        )

    rationale = "interactive lane selected for low-latency reasoning"
    if failure_fingerprint:
        rationale = "interactive lane retained because failure signal did not require recovery"
    return RouterDecision(
        intent=intent,  # type: ignore[arg-type]
        task_class=intent,
        lane="interactive",
        rationale=rationale,
        context_scope="stable_prefix",
        latency_sensitive=True,
        cache_affinity=f"{default_cache_affinity}:interactive",
        prefix_segment="stable_prefix",
        context_tokens=max(token_estimate, working_set_tokens),
        compression_pressure=compression_score,
        fact_count=max(fact_count, semantic_memory_count),
    )


def _router_model_output(
    state: dict[str, Any],
    *,
    default_route: RouterDecision,
    route_decision: dict[str, Any],
) -> tuple[RouterDecision | None, Any | None]:
    if str(route_decision.get("provider_used", "local")).strip() == "local":
        return None, None

    models_raw = state.get("_models", {})
    models = models_raw if isinstance(models_raw, dict) else {}
    slot_raw = models.get("router", {})
    slot = slot_raw if isinstance(slot_raw, dict) else {}
    provider = str(slot.get("provider", "local")).strip().lower()
    if provider in {"", "local"}:
        return None, None

    model = str(slot.get("model", "deterministic")).strip()
    if not model:
        return None, None
    temperature_raw = slot.get("temperature", 0.0)
    temperature = float(temperature_raw) if isinstance(temperature_raw, (int, float)) else 0.0

    runtime_raw = state.get("_model_provider_runtime", {})
    runtime = runtime_raw if isinstance(runtime_raw, dict) else {}

    if provider == "openai_compatible":
        oc_raw = runtime.get("openai_compatible", {})
        oc_cfg = oc_raw if isinstance(oc_raw, dict) else {}
        api_key = str(oc_cfg.get("api_key", "")).strip()
        if not api_key:
            return None, None
        base_url = str(oc_cfg.get("base_url", "https://api.openai.com/v1")).strip().rstrip("/")
        if not base_url:
            return None, None
        timeout_raw = oc_cfg.get("timeout_s", 60)
        timeout_s = int(timeout_raw) if isinstance(timeout_raw, int) and timeout_raw > 0 else 60
    else:
        do_raw = runtime.get("digitalocean", {})
        do_cfg = do_raw if isinstance(do_raw, dict) else {}
        api_key = str(do_cfg.get("api_key", "")).strip()
        if not api_key:
            return None, None
        base_url = str(do_cfg.get("base_url", "https://inference.do-ai.run/v1")).strip().rstrip("/")
        if not base_url:
            return None, None
        timeout_raw = do_cfg.get("timeout_s", 60)
        timeout_s = int(timeout_raw) if isinstance(timeout_raw, int) and timeout_raw > 0 else 60

    repo_root = Path(str(state.get("_repo_root", "."))).resolve()
    router_prompt_path = repo_root / "prompts" / "router.md"
    system_prompt = (
        "You are a router for a repo-aware coding orchestrator. "
        "Return strict JSON only."
    )
    try:
        if router_prompt_path.is_file():
            prompt_text = router_prompt_path.read_text(encoding="utf-8").strip()
            if prompt_text:
                system_prompt = prompt_text
    except OSError:
        pass

    repo_context_raw = state.get("repo_context", {})
    repo_context = dict(repo_context_raw) if isinstance(repo_context_raw, dict) else {}
    planner_context_raw = repo_context.get("planner_context", {})
    planner_context = dict(planner_context_raw) if isinstance(planner_context_raw, dict) else {}
    user_prompt = (
        "Classify the request and choose the best orchestration lane.\n"
        "Return only JSON.\n\n"
        f"request: {str(state.get('request', '')).strip()}\n"
        f"default_route: {default_route.model_dump_json()}\n"
        f"planner_context_tokens: {planner_context.get('token_estimate', 0)}\n"
        f"verification: {json.dumps(state.get('verification', {}), ensure_ascii=False)}\n"
    )

    client = InferenceClient(base_url=base_url, api_key=api_key, timeout_s=timeout_s)
    lane = str(default_route.lane).strip()
    try:
        if lane == "interactive":
            # Interactive lane: stream tokens progressively for low perceived latency.
            try:
                response = client.chat_completion_stream_sync(
                    model=model,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    temperature=max(0.0, min(temperature, 1.0)),
                    max_tokens=700,
                )
            except Exception:
                # Fall back to blocking completion if streaming fails.
                response = client.chat_completion(
                    model=model,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    temperature=max(0.0, min(temperature, 1.0)),
                    max_tokens=700,
                )
        else:
            response = client.chat_completion(
                model=model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=max(0.0, min(temperature, 1.0)),
                max_tokens=700,
            )
    finally:
        client.close()

    raw = response if isinstance(response, str) else response.text
    parsed = json.loads(_extract_json_block(raw))
    if not isinstance(parsed, dict):
        raise ValueError("router completion did not return an object")
    return RouterDecision.model_validate(parsed), response


def router(state: dict[str, Any]) -> dict[str, Any]:
    log = get_logger()
    default_route = _default_route(state)
    state_with_default = {**state, "route": default_route.model_dump()}
    state_with_default = record_model_route(
        state_with_default,
        node_name="router",
        task_class=default_route.task_class,
        model_slot="router",
    )
    state = append_event(
        state_with_default,
        kind="node",
        data={"name": "router", "phase": "start"},
    )

    route_decision = latest_model_route(state, node_name="router")
    provider = str(route_decision.get("provider", "")).strip()
    model = str(route_decision.get("model", "")).strip()

    try:
        remote_route, response = _router_model_output(
            state,
            default_route=default_route,
            route_decision=route_decision,
        )
        final_route = remote_route if remote_route is not None else default_route
        payload = final_route.model_dump()
        if provider:
            payload["provider"] = provider
        if model:
            payload["model"] = model
        payload["provider_used"] = str(route_decision.get("provider_used", "local") or "local")
        out = {**state, "route": payload, "intent": payload["intent"], "retry_target": None}
        out = record_inference_telemetry(
            out,
            node_name="router",
            provider=provider,
            model=model,
            response=response,
        )
    except Exception as exc:
        log.warning("router_model_failed", error=str(exc))
        payload = default_route.model_dump()
        if provider:
            payload["provider"] = provider
        if model:
            payload["model"] = model
        payload["provider_used"] = str(route_decision.get("provider_used", "local") or "local")
        out = {**state, "route": payload, "intent": payload["intent"], "retry_target": None}

    telemetry_raw = out.get("telemetry", {})
    telemetry = dict(telemetry_raw) if isinstance(telemetry_raw, dict) else {}
    routes_raw = telemetry.get("routing", [])
    routes = list(routes_raw) if isinstance(routes_raw, list) else []
    routes.append(dict(out.get("route", {})))
    telemetry["routing"] = routes
    out["telemetry"] = telemetry
    out = append_event(
        out,
        kind="node",
        data={
            "name": "router",
            "phase": "end",
            "lane": out.get("route", {}).get("lane", "interactive"),
        },
    )
    return out
