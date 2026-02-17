from __future__ import annotations

from lg_orch.visualize import GraphEdge, graph_mermaid


def test_graph_mermaid_basic() -> None:
    result = graph_mermaid(
        nodes=["A", "B"],
        edges=[GraphEdge("A", "B")],
    )
    assert result.startswith("flowchart LR")
    assert 'A["A"]' in result
    assert 'B["B"]' in result
    assert "A --> B" in result


def test_graph_mermaid_custom_direction() -> None:
    result = graph_mermaid(nodes=["X"], edges=[], direction="TD")
    assert result.startswith("flowchart TD")


def test_graph_mermaid_multiple_edges() -> None:
    result = graph_mermaid(
        nodes=["A", "B", "C"],
        edges=[GraphEdge("A", "B"), GraphEdge("B", "C")],
    )
    assert "A --> B" in result
    assert "B --> C" in result


def test_graph_mermaid_sanitizes_quotes() -> None:
    result = graph_mermaid(nodes=['say"hello'], edges=[])
    assert '"' not in result.split("\n")[1].split("[")[0]


def test_graph_mermaid_ends_with_newline() -> None:
    result = graph_mermaid(nodes=["A"], edges=[])
    assert result.endswith("\n")


def test_graph_edge_frozen() -> None:
    e = GraphEdge("a", "b")
    try:
        e.src = "c"  # type: ignore[misc]
        assert False, "should be frozen"  # noqa: B011
    except AttributeError:
        pass
