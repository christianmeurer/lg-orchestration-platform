from __future__ import annotations

from typing import Any

from lg_orch.state import ModelRoutingDecision


def decide_model_route(
    *,
    task_class: str,
    primary_provider: str,
    primary_model: str,
    local_provider: str,
    fallback_task_classes: tuple[str, ...],
    lane: str | None = None,
    context_tokens: int = 0,
    retry_count: int = 0,
    latency_sensitive: bool = True,
    cache_affinity: str = "",
    prefix_segment: str = "",
    interactive_context_limit: int = 1800,
    deep_planning_context_limit: int = 3200,
    recovery_retry_threshold: int = 1,
) -> ModelRoutingDecision:
    normalized_task = task_class.strip()
    normalized_lane = lane.strip() if isinstance(lane, str) else ""
    normalized_primary_provider = primary_provider.strip() or "local"
    normalized_primary_model = primary_model.strip() or "deterministic"
    normalized_local_provider = local_provider.strip() or "local"
    fallback_set = {entry.strip() for entry in fallback_task_classes if entry.strip()}

    if normalized_primary_provider == normalized_local_provider:
        return ModelRoutingDecision(
            task_class=normalized_task,
            lane=normalized_lane or "interactive",
            provider_used="local",
            provider=normalized_local_provider,
            model=normalized_primary_model,
            reason="primary_provider_is_local",
            fallback_applied=False,
            cache_affinity=cache_affinity,
            prefix_segment=prefix_segment,
            context_tokens=max(context_tokens, 0),
            retry_count=max(retry_count, 0),
            latency_sensitive=latency_sensitive,
        )

    if normalized_task in fallback_set:
        return ModelRoutingDecision(
            task_class=normalized_task,
            lane=normalized_lane or "interactive",
            provider_used="local",
            provider=normalized_local_provider,
            model=f"{normalized_local_provider}:fallback",
            reason="fallback_task_class_policy",
            fallback_applied=True,
            cache_affinity=cache_affinity,
            prefix_segment=prefix_segment,
            context_tokens=max(context_tokens, 0),
            retry_count=max(retry_count, 0),
            latency_sensitive=latency_sensitive,
        )

    if (
        normalized_lane == "interactive"
        and latency_sensitive
        and context_tokens <= max(interactive_context_limit, 1)
    ):
        return ModelRoutingDecision(
            task_class=normalized_task,
            lane="interactive",
            provider_used="local",
            provider=normalized_local_provider,
            model=f"{normalized_local_provider}:interactive",
            reason="interactive_low_latency_policy",
            fallback_applied=False,
            cache_affinity=cache_affinity,
            prefix_segment=prefix_segment,
            context_tokens=max(context_tokens, 0),
            retry_count=max(retry_count, 0),
            latency_sensitive=latency_sensitive,
        )

    if (
        normalized_lane == "deep_planning"
        or context_tokens >= max(deep_planning_context_limit, 1)
    ):
        return ModelRoutingDecision(
            task_class=normalized_task,
            lane=normalized_lane or "deep_planning",
            provider_used="remote",
            provider=normalized_primary_provider,
            model=normalized_primary_model,
            reason="high_context_capability_path",
            fallback_applied=False,
            cache_affinity=cache_affinity,
            prefix_segment=prefix_segment,
            context_tokens=max(context_tokens, 0),
            retry_count=max(retry_count, 0),
            latency_sensitive=latency_sensitive,
        )

    if normalized_lane == "recovery" and retry_count >= max(recovery_retry_threshold, 0):
        return ModelRoutingDecision(
            task_class=normalized_task,
            lane="recovery",
            provider_used="remote",
            provider=normalized_primary_provider,
            model=normalized_primary_model,
            reason="recovery_capability_path",
            fallback_applied=False,
            cache_affinity=cache_affinity,
            prefix_segment=prefix_segment,
            context_tokens=max(context_tokens, 0),
            retry_count=max(retry_count, 0),
            latency_sensitive=latency_sensitive,
        )

    return ModelRoutingDecision(
        task_class=normalized_task,
        lane=normalized_lane or "interactive",
        provider_used="remote",
        provider=normalized_primary_provider,
        model=normalized_primary_model,
        reason="primary_provider_path",
        fallback_applied=False,
        cache_affinity=cache_affinity,
        prefix_segment=prefix_segment,
        context_tokens=max(context_tokens, 0),
        retry_count=max(retry_count, 0),
        latency_sensitive=latency_sensitive,
    )


def latest_model_route(state: dict[str, Any], *, node_name: str) -> dict[str, Any]:
    telemetry_raw = state.get("telemetry", {})
    telemetry = dict(telemetry_raw) if isinstance(telemetry_raw, dict) else {}
    routes_raw = telemetry.get("model_routing", [])
    routes = list(routes_raw) if isinstance(routes_raw, list) else []
    for route in reversed(routes):
        if isinstance(route, dict) and str(route.get("node", "")) == node_name:
            return dict(route)
    return {}


def record_model_route(
    state: dict[str, Any],
    *,
    node_name: str,
    task_class: str,
    model_slot: str,
) -> dict[str, Any]:
    models_raw = state.get("_models", {})
    models = models_raw if isinstance(models_raw, dict) else {}
    slot_raw = models.get(model_slot, {})
    slot = slot_raw if isinstance(slot_raw, dict) else {}

    routing_raw = state.get("_model_routing_policy", {})
    routing = routing_raw if isinstance(routing_raw, dict) else {}
    fallback_raw = routing.get("fallback_task_classes", [])
    fallback_classes: tuple[str, ...]
    if isinstance(fallback_raw, list):
        fallback_classes = tuple(str(v).strip() for v in fallback_raw if str(v).strip())
    else:
        fallback_classes = tuple()

    route_raw = state.get("route", {})
    route = dict(route_raw) if isinstance(route_raw, dict) else {}
    budgets_raw = state.get("budgets", {})
    budgets = dict(budgets_raw) if isinstance(budgets_raw, dict) else {}
    repo_context_raw = state.get("repo_context", {})
    repo_context = dict(repo_context_raw) if isinstance(repo_context_raw, dict) else {}

    lane = str(route.get("lane", "")).strip() or None
    cache_affinity = str(
        route.get("cache_affinity", routing.get("default_cache_affinity", "workspace"))
    ).strip()
    prefix_segment = str(route.get("prefix_segment", "stable_prefix")).strip()
    retry_count = max(int(budgets.get("current_loop", 0)) - 1, 0)
    context_tokens_raw = route.get(
        "context_tokens",
        repo_context.get("planner_context", {}),
    )
    context_tokens = 0
    if isinstance(context_tokens_raw, dict):
        value = context_tokens_raw.get("token_estimate", 0)
        context_tokens = int(value) if isinstance(value, int) else 0
    elif isinstance(context_tokens_raw, int):
        context_tokens = context_tokens_raw

    latency_sensitive_raw = route.get("latency_sensitive", lane != "deep_planning")
    latency_sensitive = bool(latency_sensitive_raw)

    decision = decide_model_route(
        task_class=task_class,
        primary_provider=str(slot.get("provider", "local")),
        primary_model=str(slot.get("model", "deterministic")),
        local_provider=str(routing.get("local_provider", "local")),
        fallback_task_classes=fallback_classes,
        lane=lane,
        context_tokens=context_tokens,
        retry_count=retry_count,
        latency_sensitive=latency_sensitive,
        cache_affinity=cache_affinity,
        prefix_segment=prefix_segment,
        interactive_context_limit=int(routing.get("interactive_context_limit", 1800) or 1800),
        deep_planning_context_limit=int(
            routing.get("deep_planning_context_limit", 3200) or 3200
        ),
        recovery_retry_threshold=int(routing.get("recovery_retry_threshold", 1) or 1),
    )

    telemetry_raw = state.get("telemetry", {})
    telemetry = dict(telemetry_raw) if isinstance(telemetry_raw, dict) else {}
    routes_raw = telemetry.get("model_routing", [])
    routes = list(routes_raw) if isinstance(routes_raw, list) else []
    routes.append({"node": node_name, **decision.model_dump()})
    telemetry["model_routing"] = routes

    return {**state, "telemetry": telemetry}


def record_inference_telemetry(
    state: dict[str, Any],
    *,
    node_name: str,
    provider: str,
    model: str,
    response: Any,
) -> dict[str, Any]:
    telemetry_raw = state.get("telemetry", {})
    telemetry = dict(telemetry_raw) if isinstance(telemetry_raw, dict) else {}
    route_raw = state.get("route", {})
    route = dict(route_raw) if isinstance(route_raw, dict) else {}
    latest = latest_model_route(state, node_name=node_name)

    context_tokens_raw = latest.get("context_tokens", 0)
    context_tokens = (
        int(context_tokens_raw)
        if isinstance(context_tokens_raw, int) and not isinstance(context_tokens_raw, bool)
        else 0
    )
    retry_count_raw = latest.get("retry_count", 0)
    retry_count = (
        int(retry_count_raw)
        if isinstance(retry_count_raw, int) and not isinstance(retry_count_raw, bool)
        else 0
    )

    capture_metadata = bool(state.get("_trace_capture_model_metadata", True))
    entry: dict[str, Any] = {
        "node": node_name,
        "provider": provider,
        "model": model,
        "lane": str(route.get("lane", "interactive")),
        "provider_used": str(latest.get("provider_used", route.get("provider_used", ""))),
        "task_class": str(route.get("task_class", latest.get("task_class", ""))),
        "reason": str(latest.get("reason", "")),
        "rationale": str(route.get("rationale", latest.get("reason", ""))),
        "context_scope": str(route.get("context_scope", "")),
        "fallback_applied": bool(latest.get("fallback_applied", False)),
        "cache_affinity": str(route.get("cache_affinity", "")),
        "prefix_segment": str(route.get("prefix_segment", "stable_prefix")),
        "context_tokens": context_tokens,
        "retry_count": retry_count,
        "latency_sensitive": bool(
            latest.get("latency_sensitive", route.get("latency_sensitive", True))
        ),
        "latency_ms": 0,
    }
    if response is not None and not isinstance(response, str):
        latency_ms = getattr(response, "latency_ms", 0)
        entry["latency_ms"] = int(latency_ms) if isinstance(latency_ms, int) else 0
        provider_used = str(getattr(response, "provider", "")).strip()
        model_used = str(getattr(response, "model", "")).strip()
        if provider_used:
            entry["provider"] = provider_used
        if model_used:
            entry["model"] = model_used

        usage_raw = getattr(response, "usage", {})
        if isinstance(usage_raw, dict) and usage_raw:
            entry["usage"] = usage_raw

        cache_metadata_raw = getattr(response, "cache_metadata", {})
        if isinstance(cache_metadata_raw, dict) and cache_metadata_raw:
            entry["cache_metadata"] = cache_metadata_raw

        if capture_metadata:
            headers_raw = getattr(response, "headers", {})
            if isinstance(headers_raw, dict) and headers_raw:
                entry["headers"] = headers_raw

    inference_raw = telemetry.get("inference", [])
    inference = list(inference_raw) if isinstance(inference_raw, list) else []
    inference.append(entry)
    telemetry["inference"] = inference
    return {**state, "telemetry": telemetry}


def tool_routing_metadata(state: dict[str, Any], *, stage: str) -> dict[str, Any]:
    route_raw = state.get("route", {})
    route = dict(route_raw) if isinstance(route_raw, dict) else {}
    telemetry_raw = state.get("telemetry", {})
    telemetry = dict(telemetry_raw) if isinstance(telemetry_raw, dict) else {}
    routes_raw = telemetry.get("model_routing", [])
    routes = list(routes_raw) if isinstance(routes_raw, list) else []
    latest = dict(routes[-1]) if routes and isinstance(routes[-1], dict) else {}
    return {
        "stage": stage,
        "lane": str(route.get("lane", latest.get("lane", "interactive"))),
        "provider": str(latest.get("provider", route.get("provider", ""))),
        "model": str(latest.get("model", route.get("model", ""))),
        "task_class": str(route.get("task_class", latest.get("task_class", ""))),
        "cache_affinity": str(route.get("cache_affinity", latest.get("cache_affinity", ""))),
        "prefix_segment": str(route.get("prefix_segment", latest.get("prefix_segment", "stable_prefix"))),
    }

