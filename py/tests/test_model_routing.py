from __future__ import annotations

from lg_orch.model_routing import (
    DiversityRoutingPolicy,
    SlaRoutingPolicy,
    TemperatureDiversityMixin,
    decide_model_route,
    get_routing_policy,
    record_inference_telemetry,
    record_model_route,
)


def test_decide_model_route_uses_local_fallback_for_configured_task_class() -> None:
    decision = decide_model_route(
        task_class="summarization",
        primary_provider="remote_openai",
        primary_model="gpt-4.1",
        local_provider="local",
        fallback_task_classes=("summarization", "lint_reflection"),
    )
    assert decision.provider_used == "local"
    assert decision.fallback_applied is True
    assert decision.reason == "fallback_task_class_policy"


def test_decide_model_route_keeps_remote_for_non_fallback_task_class() -> None:
    decision = decide_model_route(
        task_class="research",
        primary_provider="remote_openai",
        primary_model="gpt-4.1",
        local_provider="local",
        fallback_task_classes=("summarization",),
    )
    assert decision.provider_used == "remote"
    assert decision.fallback_applied is False
    assert decision.reason == "primary_provider_path"


def test_decide_model_route_prefers_remote_for_recovery_lane() -> None:
    decision = decide_model_route(
        task_class="verification_failed",
        primary_provider="remote_openai",
        primary_model="gpt-4.1",
        local_provider="local",
        fallback_task_classes=("summarization",),
        lane="recovery",
        retry_count=0,
    )
    assert decision.provider_used == "remote"
    assert decision.reason == "recovery_capability_path"


def test_decide_model_route_uses_remote_when_compression_pressure_exists() -> None:
    decision = decide_model_route(
        task_class="analysis",
        primary_provider="remote_openai",
        primary_model="gpt-4.1",
        local_provider="local",
        fallback_task_classes=(),
        lane="interactive",
        context_tokens=900,
        latency_sensitive=True,
        compression_pressure=2,
    )
    assert decision.provider_used == "remote"
    assert decision.reason == "compression_pressure_capability_path"


def test_record_model_route_appends_telemetry_marker() -> None:
    state = {
        "telemetry": {},
        "_models": {
            "router": {"provider": "remote_openai", "model": "gpt-4.1", "temperature": 0.0},
            "planner": {"provider": "remote_openai", "model": "gpt-4.1", "temperature": 0.0},
        },
        "_model_routing_policy": {
            "local_provider": "local",
            "fallback_task_classes": ["context_condensation"],
        },
    }
    out = record_model_route(
        state,
        node_name="planner",
        task_class="context_condensation",
        model_slot="planner",
    )

    routing = out["telemetry"]["model_routing"]
    assert len(routing) == 1
    assert routing[0]["node"] == "planner"
    assert routing[0]["provider_used"] == "local"
    assert routing[0]["fallback_applied"] is True


def test_record_inference_telemetry_captures_route_metadata() -> None:
    from typing import ClassVar

    class _Response:
        latency_ms = 42
        provider = "remote_openai"
        model = "gpt-4.1"
        usage: ClassVar[dict[str, int]] = {"input_tokens": 120, "output_tokens": 12}
        cache_metadata: ClassVar[dict[str, str]] = {"x-cache-hit": "true"}
        headers: ClassVar[dict[str, str]] = {"x-request-id": "req-123"}

    state = {
        "route": {
            "lane": "deep_planning",
            "task_class": "deep_planning",
            "rationale": "context requires a stronger planner",
            "context_scope": "stable_prefix",
            "cache_affinity": "workspace:planner:1",
            "prefix_segment": "stable_prefix",
            "compression_pressure": 2,
            "fact_count": 4,
        },
        "telemetry": {
            "model_routing": [
                {
                    "node": "planner",
                    "provider_used": "remote",
                    "task_class": "deep_planning",
                    "reason": "high_context_capability_path",
                    "fallback_applied": False,
                    "context_tokens": 2048,
                    "retry_count": 1,
                    "latency_sensitive": False,
                }
            ]
        },
        "_trace_capture_model_metadata": True,
    }

    out = record_inference_telemetry(
        state,
        node_name="planner",
        provider="remote_openai",
        model="gpt-4.1",
        response=_Response(),
    )

    entry = out["telemetry"]["inference"][-1]
    assert entry["provider_used"] == "remote"
    assert entry["task_class"] == "deep_planning"
    assert entry["reason"] == "high_context_capability_path"
    assert entry["rationale"] == "context requires a stronger planner"
    assert entry["context_scope"] == "stable_prefix"
    assert entry["context_tokens"] == 2048
    assert entry["compression_pressure"] == 2
    assert entry["fact_count"] == 4
    assert entry["retry_count"] == 1
    assert entry["latency_sensitive"] is False
    assert entry["usage"]["input_tokens"] == 120
    assert entry["cache_metadata"]["x-cache-hit"] == "true"


def test_get_routing_policy_returns_sla_by_default() -> None:
    """When LG_MODEL_DIVERSITY is unset, get_routing_policy returns SlaRoutingPolicy or None."""
    import os

    os.environ.pop("LG_MODEL_DIVERSITY", None)
    policy = get_routing_policy()
    assert policy is None  # no sla_config provided, no diversity


def test_get_routing_policy_returns_diversity_when_enabled() -> None:
    """When LG_MODEL_DIVERSITY=true, get_routing_policy returns DiversityRoutingPolicy."""
    import os

    os.environ["LG_MODEL_DIVERSITY"] = "true"
    try:
        policy = get_routing_policy(diversity_models=["model-a", "model-b"])
        assert isinstance(policy, DiversityRoutingPolicy)
        assert policy.models == ["model-a", "model-b"]
    finally:
        os.environ.pop("LG_MODEL_DIVERSITY", None)


def test_get_routing_policy_falls_back_to_sla_when_diversity_disabled() -> None:
    """When LG_MODEL_DIVERSITY is false, an sla_config yields SlaRoutingPolicy."""
    import os

    from lg_orch.model_routing import SlaConfig, SlaEntry

    os.environ.pop("LG_MODEL_DIVERSITY", None)
    cfg = SlaConfig(entries=[SlaEntry(model_id="m1", threshold_p95_s=2.0, fallback_model_id="m2")])
    policy = get_routing_policy(sla_config=cfg, diversity_models=["model-a"])
    assert isinstance(policy, SlaRoutingPolicy)


def test_diversity_routing_policy_round_robin() -> None:
    """DiversityRoutingPolicy cycles through models in order."""
    policy = DiversityRoutingPolicy(models=["alpha", "beta", "gamma"])
    results = [policy.select_model() for _ in range(7)]
    assert results == ["alpha", "beta", "gamma", "alpha", "beta", "gamma", "alpha"]


def test_decide_model_route_interactive_low_latency() -> None:
    """Interactive lane with low tokens and latency-sensitive returns local interactive."""
    decision = decide_model_route(
        task_class="analysis",
        primary_provider="remote_openai",
        primary_model="gpt-4.1",
        local_provider="local",
        fallback_task_classes=(),
        lane="interactive",
        context_tokens=500,
        latency_sensitive=True,
    )
    assert decision.provider_used == "local"
    assert decision.reason == "interactive_low_latency_policy"


def test_latest_model_route_returns_empty_for_no_match() -> None:
    from lg_orch.model_routing import latest_model_route

    state = {"telemetry": {"model_routing": [{"node": "coder", "model": "x"}]}}
    assert latest_model_route(state, node_name="planner") == {}


def test_latest_model_route_returns_empty_for_missing_telemetry() -> None:
    from lg_orch.model_routing import latest_model_route

    assert latest_model_route({}, node_name="planner") == {}


def test_record_model_route_handles_non_list_fallback_classes() -> None:
    """When fallback_task_classes is not a list, should use empty tuple."""
    state = {
        "telemetry": {},
        "_models": {"planner": {"provider": "local", "model": "det"}},
        "_model_routing_policy": {
            "local_provider": "local",
            "fallback_task_classes": "not_a_list",
        },
    }
    out = record_model_route(
        state, node_name="planner", task_class="analysis", model_slot="planner"
    )
    routing = out["telemetry"]["model_routing"]
    assert len(routing) == 1


def test_diversity_routing_policy_reset() -> None:
    """reset() restarts the round-robin from the first model."""
    policy = DiversityRoutingPolicy(models=["alpha", "beta"])
    policy.select_model()
    policy.select_model()
    policy.reset()
    assert policy.select_model() == "alpha"


def test_temperature_diversity_mixin_cycles_schedule() -> None:
    """next_temperature() cycles through the full schedule in order."""
    schedule = TemperatureDiversityMixin._TEMPERATURE_SCHEDULE
    policy = DiversityRoutingPolicy(models=["m"])
    temps = [policy.next_temperature() for _ in range(len(schedule))]
    assert temps == schedule


def test_temperature_diversity_mixin_wraps_around() -> None:
    """next_temperature() wraps around after exhausting the schedule."""
    schedule = TemperatureDiversityMixin._TEMPERATURE_SCHEDULE
    policy = DiversityRoutingPolicy(models=["m"])
    # exhaust one full cycle
    for _ in range(len(schedule)):
        policy.next_temperature()
    # next value should restart from the beginning
    assert policy.next_temperature() == schedule[0]


def test_temperature_diversity_mixin_reset() -> None:
    """reset_temperature() restarts the schedule from index 0."""
    schedule = TemperatureDiversityMixin._TEMPERATURE_SCHEDULE
    policy = DiversityRoutingPolicy(models=["m"])
    policy.next_temperature()
    policy.next_temperature()
    policy.reset_temperature()
    assert policy.next_temperature() == schedule[0]


def test_diversity_policy_inherits_temperature_mixin() -> None:
    """DiversityRoutingPolicy is an instance of TemperatureDiversityMixin."""
    policy = DiversityRoutingPolicy(models=["x"])
    assert isinstance(policy, TemperatureDiversityMixin)
