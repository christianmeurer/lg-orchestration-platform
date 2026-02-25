from __future__ import annotations

from typing import Any

from langgraph.graph import END, StateGraph

from lg_orch.nodes import (
    context_builder,
    executor,
    ingest,
    planner,
    policy_gate,
    reporter,
    verifier,
)
from lg_orch.visualize import GraphEdge, graph_mermaid


def route_after_verifier(state: dict[str, Any]) -> str:
    verification = state.get("verification", {})
    if verification.get("ok"):
        return "reporter"

    budgets = state.get("budgets", {})
    current_loop = budgets.get("current_loop", 1)
    max_loops = budgets.get("max_loops", 3)

    if current_loop >= max_loops:
        return "reporter"

    return "planner"


def build_graph() -> Any:
    g: StateGraph = StateGraph(dict)
    g.add_node("ingest", ingest)
    g.add_node("policy_gate", policy_gate)
    g.add_node("context_builder", context_builder)
    g.add_node("planner", planner)
    g.add_node("executor", executor)
    g.add_node("verifier", verifier)
    g.add_node("reporter", reporter)

    g.set_entry_point("ingest")
    g.add_edge("ingest", "policy_gate")
    g.add_edge("policy_gate", "context_builder")
    g.add_edge("context_builder", "planner")
    g.add_edge("planner", "executor")
    g.add_edge("executor", "verifier")
    g.add_conditional_edges(
        "verifier", route_after_verifier, {"reporter": "reporter", "planner": "planner"}
    )
    g.add_edge("reporter", END)
    return g.compile()


def export_mermaid() -> str:
    nodes = [
        "ingest",
        "policy_gate",
        "context_builder",
        "planner",
        "executor",
        "verifier",
        "reporter",
        "END",
    ]
    edges = [
        GraphEdge("ingest", "policy_gate"),
        GraphEdge("policy_gate", "context_builder"),
        GraphEdge("context_builder", "planner"),
        GraphEdge("planner", "executor"),
        GraphEdge("executor", "verifier"),
        GraphEdge("verifier", "reporter"),
        GraphEdge("verifier", "planner"),
        GraphEdge("reporter", "END"),
    ]
    return graph_mermaid(nodes=nodes, edges=edges)
