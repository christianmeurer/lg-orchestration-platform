from __future__ import annotations

from lg_orch.nodes.router import _default_route


def test_default_route_prefers_deep_planning_when_semantic_memory_recalls_multiple_matches() -> None:
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
