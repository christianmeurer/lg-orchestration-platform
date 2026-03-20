from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest

from lg_orch.model_routing import (
    LatencyWindow,
    SlaConfig,
    SlaEntry,
    SlaRoutingPolicy,
    build_sla_policy,
)


# ---------------------------------------------------------------------------
# LatencyWindow — p95 with fewer than 5 samples returns None
# ---------------------------------------------------------------------------


def test_latency_window_p95_none_below_min_samples() -> None:
    w = LatencyWindow("gpt-4o")
    assert w.p95() is None

    for val in [0.1, 0.2, 0.3, 0.4]:
        w.record(val)
    assert w.p95() is None


# ---------------------------------------------------------------------------
# LatencyWindow — correct p95 with known samples
# ---------------------------------------------------------------------------


def test_latency_window_p95_known_distribution() -> None:
    w = LatencyWindow("gpt-4o", window_size=200)
    for _ in range(95):
        w.record(0.1)
    for _ in range(5):
        w.record(1.0)

    p95 = w.p95()
    assert p95 is not None
    # The 95th percentile index into 100 sorted samples = 95, which is 1.0
    assert p95 == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# LatencyWindow — circular buffer evicts oldest at capacity
# ---------------------------------------------------------------------------


def test_latency_window_circular_eviction() -> None:
    w = LatencyWindow("gpt-4o", window_size=5)
    for i in range(5):
        w.record(float(i))  # 0.0, 1.0, 2.0, 3.0, 4.0

    # Add one more: oldest (0.0) should be evicted
    w.record(10.0)
    assert w.sample_count() == 5

    # After eviction the minimum should no longer be 0.0
    p95 = w.p95()
    assert p95 is not None
    # Samples now: [1.0, 2.0, 3.0, 4.0, 10.0]
    # sorted: [1.0, 2.0, 3.0, 4.0, 10.0], idx = int(5*0.95)=4 → 10.0
    assert p95 == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# SlaRoutingPolicy.select_model — returns requested model when under threshold
# ---------------------------------------------------------------------------


def test_select_model_under_threshold() -> None:
    policy = SlaRoutingPolicy(
        thresholds={"gpt-4o": 2.0},
        fallbacks={"gpt-4o": "gpt-3.5-turbo"},
    )
    for _ in range(10):
        policy.record_latency("gpt-4o", 0.5)

    assert policy.select_model("gpt-4o") == "gpt-4o"


# ---------------------------------------------------------------------------
# SlaRoutingPolicy.select_model — returns fallback when p95 exceeds threshold
# ---------------------------------------------------------------------------


def test_select_model_over_threshold_returns_fallback() -> None:
    policy = SlaRoutingPolicy(
        thresholds={"gpt-4o": 0.3},
        fallbacks={"gpt-4o": "gpt-3.5-turbo"},
    )
    for _ in range(10):
        policy.record_latency("gpt-4o", 1.0)

    assert policy.select_model("gpt-4o") == "gpt-3.5-turbo"


# ---------------------------------------------------------------------------
# SlaRoutingPolicy.select_model — returns requested model when no window yet
# ---------------------------------------------------------------------------


def test_select_model_no_window_returns_requested() -> None:
    policy = SlaRoutingPolicy(
        thresholds={"gpt-4o": 2.0},
        fallbacks={"gpt-4o": "gpt-3.5-turbo"},
    )
    assert policy.select_model("gpt-4o") == "gpt-4o"


# ---------------------------------------------------------------------------
# SlaRoutingPolicy.degraded_models — returns correct list
# ---------------------------------------------------------------------------


def test_degraded_models_correct_list() -> None:
    policy = SlaRoutingPolicy(
        thresholds={"gpt-4o": 0.3, "claude-3": 2.0},
        fallbacks={"gpt-4o": "gpt-3.5-turbo", "claude-3": "claude-instant"},
    )
    for _ in range(10):
        policy.record_latency("gpt-4o", 1.0)   # over threshold
        policy.record_latency("claude-3", 0.1)  # under threshold

    degraded = policy.degraded_models()
    assert "gpt-4o" in degraded
    assert "claude-3" not in degraded


# ---------------------------------------------------------------------------
# build_sla_policy — returns None for empty config
# ---------------------------------------------------------------------------


def test_build_sla_policy_empty_config_returns_none() -> None:
    config = SlaConfig(entries=[])
    assert build_sla_policy(config) is None


# ---------------------------------------------------------------------------
# build_sla_policy — returns populated policy for non-empty config
# ---------------------------------------------------------------------------


def test_build_sla_policy_populates_policy() -> None:
    config = SlaConfig(
        entries=[
            SlaEntry(
                model_id="gpt-4o",
                threshold_p95_s=1.0,
                fallback_model_id="gpt-3.5-turbo",
            )
        ]
    )
    policy = build_sla_policy(config)
    assert policy is not None
    # No samples yet → returns requested model unchanged
    assert policy.select_model("gpt-4o") == "gpt-4o"


# ---------------------------------------------------------------------------
# Thread-safety: 20 threads each record 50 samples → sample_count == 200 cap
# ---------------------------------------------------------------------------


def test_latency_window_thread_safety() -> None:
    w = LatencyWindow("gpt-4o", window_size=200)

    def worker() -> None:
        for _ in range(50):
            w.record(0.1)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert w.sample_count() == 200


# ---------------------------------------------------------------------------
# InferenceClient — model substitution occurs before HTTP call
# ---------------------------------------------------------------------------


def test_inference_client_model_substitution() -> None:
    from lg_orch.model_routing import SlaRoutingPolicy
    from lg_orch.tools.inference_client import InferenceClient, InferenceResponse

    # Build a policy that always routes "gpt-4o" → "gpt-3.5-turbo"
    policy = SlaRoutingPolicy(
        thresholds={"gpt-4o": 0.001},
        fallbacks={"gpt-4o": "gpt-3.5-turbo"},
    )
    # Force window to have enough high-latency samples to exceed threshold
    for _ in range(10):
        policy.record_latency("gpt-4o", 9.0)

    fake_response = InferenceResponse(
        text="hello",
        latency_ms=100,
        provider="openai",
        model="gpt-3.5-turbo",
    )

    with patch.object(
        InferenceClient,
        "_execute_request",
        return_value=fake_response,
    ) as mock_exec:
        client = InferenceClient(
            base_url="http://localhost:9999",
            api_key="test-key",
            sla_policy=policy,
        )
        result = client.chat_completion(
            model="gpt-4o",
            system_prompt="sys",
            user_prompt="hi",
            temperature=0.0,
        )

    # The actual HTTP call must have used the fallback model
    called_model = mock_exec.call_args.kwargs["model"]
    assert called_model == "gpt-3.5-turbo"
    assert result.text == "hello"
