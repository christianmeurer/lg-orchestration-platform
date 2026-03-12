from __future__ import annotations

from lg_orch.model_routing import decide_model_route, record_inference_telemetry, record_model_route


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
    class _Response:
        latency_ms = 42
        provider = "remote_openai"
        model = "gpt-4.1"
        usage = {"input_tokens": 120, "output_tokens": 12}
        cache_metadata = {"x-cache-hit": "true"}
        headers = {"x-request-id": "req-123"}

    state = {
        "route": {
            "lane": "deep_planning",
            "task_class": "deep_planning",
            "rationale": "context requires a stronger planner",
            "context_scope": "stable_prefix",
            "cache_affinity": "workspace:planner:1",
            "prefix_segment": "stable_prefix",
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
    assert entry["retry_count"] == 1
    assert entry["latency_sensitive"] is False
    assert entry["usage"]["input_tokens"] == 120
    assert entry["cache_metadata"]["x-cache-hit"] == "true"

