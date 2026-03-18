from __future__ import annotations

from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, StateGraph

from lg_orch.nodes import (
    coder,
    context_builder,
    executor,
    ingest,
    planner,
    policy_gate,
    reporter,
    router,
    verifier,
)
from lg_orch.visualize import GraphEdge, graph_mermaid


def route_after_policy_gate(state: dict[str, Any]) -> str:
    halt_reason = str(state.get("halt_reason", "")).strip()
    if halt_reason in {"max_loops_exhausted", "plan_max_iterations_exhausted"}:
        return "reporter"

    if bool(state.get("context_reset_requested", False)):
        return "context_builder"

    retry_target = state.get("retry_target")
    if retry_target == "router":
        return "router"
    if retry_target == "planner":
        return "planner"
    if retry_target == "coder":
        return "coder"
    if retry_target == "context_builder":
        return "context_builder"

    return "context_builder"


def route_after_verifier(state: dict[str, Any]) -> str:
    verification = state.get("verification", {})
    if verification.get("ok"):
        return "reporter"

    return "policy_gate"


def build_graph(*, checkpointer: BaseCheckpointSaver[Any] | None = None) -> Any:
    g: StateGraph = StateGraph(dict)
    g.add_node("ingest", ingest)
    g.add_node("policy_gate", policy_gate)
    g.add_node("context_builder", context_builder)
    g.add_node("router", router)
    g.add_node("planner", planner)
    g.add_node("coder", coder)
    g.add_node("executor", executor)
    g.add_node("verifier", verifier)
    g.add_node("reporter", reporter)

    g.set_entry_point("ingest")
    g.add_edge("ingest", "policy_gate")
    g.add_conditional_edges(
        "policy_gate",
        route_after_policy_gate,
        {
            "context_builder": "context_builder",
            "router": "router",
            "planner": "planner",
            "coder": "coder",
            "reporter": "reporter",
        },
    )
    g.add_edge("context_builder", "router")
    g.add_edge("router", "planner")
    g.add_edge("planner", "coder")
    g.add_edge("coder", "executor")
    g.add_edge("executor", "verifier")
    g.add_conditional_edges(
        "verifier",
        route_after_verifier,
        {"reporter": "reporter", "policy_gate": "policy_gate"},
    )
    g.add_edge("reporter", END)
    if checkpointer is not None:
        return g.compile(checkpointer=checkpointer)
    return g.compile()


def export_mermaid() -> str:
    nodes = [
        "ingest",
        "policy_gate",
        "context_builder",
        "router",
        "planner",
        "coder",
        "executor",
        "verifier",
        "reporter",
        "END",
    ]
    edges = [
        GraphEdge("ingest", "policy_gate"),
        GraphEdge("policy_gate", "context_builder"),
        GraphEdge("policy_gate", "router"),
        GraphEdge("policy_gate", "planner"),
        GraphEdge("policy_gate", "coder"),
        GraphEdge("policy_gate", "reporter"),
        GraphEdge("context_builder", "router"),
        GraphEdge("router", "planner"),
        GraphEdge("planner", "coder"),
        GraphEdge("coder", "executor"),
        GraphEdge("executor", "verifier"),
        GraphEdge("verifier", "reporter"),
        GraphEdge("verifier", "policy_gate"),
        GraphEdge("reporter", "END"),
    ]
    return graph_mermaid(nodes=nodes, edges=edges)
