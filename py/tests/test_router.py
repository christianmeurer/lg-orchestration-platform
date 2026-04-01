from __future__ import annotations

import pytest

from lg_orch.nodes.router import _classify_intent, _default_route


def test_default_route_prefers_deep_planning_when_semantic_memory_recalls_multiple_matches() -> (
    None
):
    route = _default_route(
        {
            "request": "summarize the relevant fixes for approval and resume",
            "repo_context": {
                "planner_context": {
                    "token_estimate": 300,
                    "working_set_token_estimate": 120,
                    "compression_pressure": 0,
                    "fact_count": 1,
                    "semantic_memory_count": 2,
                },
                "semantic_memories": [
                    {"kind": "approval_history", "summary": "approved apply patch"},
                    {"kind": "loop_summary", "summary": "resume from checkpoint after approval"},
                ],
                "compression": {"pressure": {"overall": {"score": 0}}},
            },
            "facts": [],
            "budgets": {"current_loop": 0},
            "_model_routing_policy": {
                "interactive_context_limit": 1800,
                "default_cache_affinity": "workspace",
            },
        }
    )
    assert route.lane == "deep_planning"
    assert route.rationale == "semantic memory recall indicates deeper planning is needed"


def test_default_route_keeps_interactive_when_semantic_memory_is_absent() -> None:
    route = _default_route(
        {
            "request": "summarize the repository",
            "repo_context": {
                "planner_context": {
                    "token_estimate": 200,
                    "working_set_token_estimate": 80,
                    "compression_pressure": 0,
                    "fact_count": 0,
                },
                "compression": {"pressure": {"overall": {"score": 0}}},
            },
            "facts": [],
            "budgets": {"current_loop": 0},
            "_model_routing_policy": {
                "interactive_context_limit": 1800,
                "default_cache_affinity": "workspace",
            },
        }
    )
    assert route.lane == "interactive"


# ---------------------------------------------------------------------------
# _classify_intent
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "request_text,expected_intent",
    [
        ("implement the login feature", "code_change"),
        ("add a new endpoint", "code_change"),
        ("fix the broken test", "code_change"),
        ("refactor auth module", "code_change"),
        ("debug the panic in handler", "debug"),
        ("there is an error in the parser", "debug"),
        ("exception thrown during startup", "debug"),
        ("research the latest frameworks", "research"),
        ("compare React vs Vue", "research"),
        ("why does this fail?", "question"),
        ("how does the router work?", "question"),
        ("explain the state machine", "question"),
        ("look at the logs", "analysis"),
    ],
)
def test_classify_intent(request_text: str, expected_intent: str) -> None:
    assert _classify_intent(request_text) == expected_intent


# ---------------------------------------------------------------------------
# _default_route — recovery lane
# ---------------------------------------------------------------------------


def _minimal_state(**overrides: object) -> dict:
    base = {
        "request": "test",
        "repo_context": {
            "planner_context": {
                "token_estimate": 200,
                "working_set_token_estimate": 80,
                "compression_pressure": 0,
                "fact_count": 0,
            },
            "compression": {"pressure": {"overall": {"score": 0}}},
        },
        "facts": [],
        "budgets": {"current_loop": 0},
        "_model_routing_policy": {
            "interactive_context_limit": 1800,
            "default_cache_affinity": "workspace",
        },
    }
    base.update(overrides)
    return base


def test_default_route_recovery_lane_on_retry_target() -> None:
    route = _default_route(_minimal_state(retry_target="router"))
    assert route.lane == "recovery"
    assert "recovery" in route.rationale


def test_default_route_recovery_lane_on_verification_recovery() -> None:
    route = _default_route(
        _minimal_state(
            verification={
                "ok": False,
                "recovery": {"failure_class": "test_failure", "context_scope": "working_set"},
            }
        )
    )
    assert route.lane == "recovery"
    assert route.task_class == "test_failure"


def test_default_route_deep_planning_for_code_change() -> None:
    route = _default_route(_minimal_state(request="implement a new feature"))
    assert route.lane == "deep_planning"


def test_default_route_deep_planning_for_high_token_estimate() -> None:
    route = _default_route(
        _minimal_state(
            request="summarize something",
            repo_context={
                "planner_context": {
                    "token_estimate": 5000,
                    "working_set_token_estimate": 80,
                    "compression_pressure": 0,
                    "fact_count": 0,
                },
                "compression": {"pressure": {"overall": {"score": 0}}},
            },
        )
    )
    assert route.lane == "deep_planning"


def test_default_route_deep_planning_for_compression_pressure() -> None:
    route = _default_route(
        _minimal_state(
            request="summarize something",
            repo_context={
                "planner_context": {
                    "token_estimate": 200,
                    "working_set_token_estimate": 80,
                    "compression_pressure": 3,
                    "fact_count": 0,
                },
                "compression": {"pressure": {"overall": {"score": 3}}},
            },
        )
    )
    assert route.lane == "deep_planning"
    assert "compression" in route.rationale


def test_default_route_deep_planning_for_high_fact_count() -> None:
    route = _default_route(
        _minimal_state(
            request="summarize something",
            repo_context={
                "planner_context": {
                    "token_estimate": 200,
                    "working_set_token_estimate": 80,
                    "compression_pressure": 0,
                    "fact_count": 5,
                },
                "compression": {"pressure": {"overall": {"score": 0}}},
            },
        )
    )
    assert route.lane == "deep_planning"
    assert "recovery memory" in route.rationale


def test_default_route_interactive_with_failure_fingerprint() -> None:
    route = _default_route(
        _minimal_state(
            request="summarize something",
            verification={"failure_fingerprint": "fp-123"},
        )
    )
    assert route.lane == "interactive"
    assert "failure signal" in route.rationale
