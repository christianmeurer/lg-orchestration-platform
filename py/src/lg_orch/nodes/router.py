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

    retry_target = str(state.get("retry_target", "")).strip()
    failure_fingerprint = str(verification.get("failure_fingerprint", "")).strip()
    current_loop_raw = state.get("budgets", {})
    current_loop_state = dict(current_loop_raw) if isinstance(current_loop_raw, dict) else {}
    current_loop = int(current_loop_state.get("current_loop", 0) or 0)

    if retry_target == "router" or recovery:
        failure_class = str(recovery.get("failure_class", verification.get("failure_class", "verification_failed")))
        return RouterDecision(
            intent=intent,
            task_class=failure_class or "recovery",
            lane="recovery",
            rationale="verification requested a recovery route",
            context_scope=str(recovery.get("context_scope", "working_set")) or "working_set",
            latency_sensitive=False,
            cache_affinity=f"{default_cache_affinity}:recovery",
            prefix_segment="recovery_working_set",
        )

    if intent in {"code_change", "refactor", "debug"} or token_estimate > interactive_limit:
        return RouterDecision(
            intent=intent,
            task_class="deep_planning",
            lane="deep_planning",
            rationale="request complexity or context size requires deeper planning",
            context_scope="stable_prefix",
            latency_sensitive=False,
            cache_affinity=f"{default_cache_affinity}:planner:{current_loop}",
            prefix_segment="stable_prefix",
        )

    rationale = "interactive lane selected for low-latency reasoning"
    if failure_fingerprint:
        rationale = "interactive lane retained because failure signal did not require recovery"
    return RouterDecision(
        intent=intent,
        task_class=intent,
        lane="interactive",
        rationale=rationale,
        context_scope="stable_prefix",
        latency_sensitive=True,
        cache_affinity=f"{default_cache_affinity}:interactive",
        prefix_segment="stable_prefix",
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
    try:
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
