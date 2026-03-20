# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Christian Meurer — https://github.com/christianmeurer/Lula
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
from lg_orch.state import OrchState
from lg_orch.visualize import GraphEdge, graph_mermaid


def _make_traced_node(node_fn: Any, node_name: str) -> Any:
    """Wrap a LangGraph node function with an OTel child span.

    The span is named ``node.<node_name>`` and carries three attributes:
    ``graph.node``, ``graph.run_id``, and ``graph.lane`` (the ``_lane``
    field in state, when present).
    """

    def _traced(state: OrchState) -> Any:
        try:
            from opentelemetry import trace as _otel_trace

            tracer = _otel_trace.get_tracer("lg_orch.graph")
            run_id = str(state.model_extra.get("_run_id", ""))
            lane = str(state.model_extra.get("_lane", ""))
            with tracer.start_as_current_span(
                f"node.{node_name}",
                attributes={
                    "graph.node": node_name,
                    "graph.run_id": run_id,
                    "graph.lane": lane,
                },
            ):
                return node_fn(state)
        except Exception:  # noqa: BLE001
            # OTel must never break graph execution.
            return node_fn(state)

    # Preserve the original callable's identity for LangGraph introspection.
    _traced.__name__ = getattr(node_fn, "__name__", node_name)
    _traced.__qualname__ = getattr(node_fn, "__qualname__", node_name)
    return _traced


def route_after_policy_gate(state: OrchState) -> str:
    halt_reason = state.halt_reason.strip()
    if halt_reason in {"max_loops_exhausted", "plan_max_iterations_exhausted"}:
        return "reporter"

    if state.context_reset_requested:
        return "context_builder"

    retry_target = state.retry_target
    if retry_target == "router":
        return "router"
    if retry_target == "planner":
        return "planner"
    if retry_target == "coder":
        return "coder"
    if retry_target == "context_builder":
        return "context_builder"

    return "context_builder"


def route_after_verifier(state: OrchState) -> str:
    if state.verification is not None and state.verification.ok:
        return "reporter"

    return "policy_gate"


def build_graph(*, checkpointer: BaseCheckpointSaver[Any] | None = None) -> Any:
    g: StateGraph = StateGraph(OrchState)
    g.add_node("ingest", _make_traced_node(ingest, "ingest"))
    g.add_node("policy_gate", _make_traced_node(policy_gate, "policy_gate"))
    g.add_node("context_builder", _make_traced_node(context_builder, "context_builder"))
    g.add_node("router", _make_traced_node(router, "router"))
    g.add_node("planner", _make_traced_node(planner, "planner"))
    g.add_node("coder", _make_traced_node(coder, "coder"))
    g.add_node("executor", _make_traced_node(executor, "executor"))
    g.add_node("verifier", _make_traced_node(verifier, "verifier"))
    g.add_node("reporter", _make_traced_node(reporter, "reporter"))

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
