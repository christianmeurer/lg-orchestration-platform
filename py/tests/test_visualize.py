from __future__ import annotations

from typing import Any

from lg_orch.visualize import (
    GraphEdge,
    graph_mermaid,
    render_run_header,
    render_run_viewer_spa,
    render_timeline,
    render_tool_results,
    render_trace_dashboard,
    render_trace_dashboard_html,
    render_trace_site_index_html,
)


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


def test_render_run_header_contains_request() -> None:
    result = render_run_header(request="Inspect repo", intent="analysis")
    assert "Lula Console" in result
    assert "request: Inspect repo" in result
    assert "intent: analysis" in result


def test_render_timeline_empty() -> None:
    result = render_timeline([])
    assert "Timeline" in result
    assert "No events captured." in result


def test_render_timeline_renders_progress() -> None:
    events: list[dict[str, Any]] = [
        {"ts_ms": 1000, "kind": "ingest", "data": {}},
        {"ts_ms": 2500, "kind": "planner", "data": {}},
    ]
    result = render_timeline(events)
    assert "+  0.00s" in result
    assert "planner" in result


def test_render_tool_results_formats_ok_and_err() -> None:
    tool_results: list[dict[str, Any]] = [
        {"tool": "read_file", "ok": True},
        {"tool": "execute_command", "ok": False},
    ]
    result = render_tool_results(tool_results)
    assert "[OK] read_file" in result
    assert "[ERR] execute_command" in result


def test_render_trace_dashboard_combines_sections() -> None:
    payload: dict[str, Any] = {
        "request": "Find bug",
        "intent": "debug",
        "verification": {"ok": False, "acceptance_ok": False},
        "approval": {"pending": True, "history": [{"decision": "approved"}]},
        "halt_reason": "plan_max_iterations_exhausted",
        "events": [{"ts_ms": 1, "kind": "ingest", "data": {}}],
        "tool_results": [{"tool": "search_files", "ok": True}],
        "final": "Done",
    }
    result = render_trace_dashboard(payload)
    assert "Lula Console" in result
    assert "Timeline" in result
    assert "Tool Results" in result
    assert "Final Output" in result
    assert "Verification" in result
    assert "acceptance: failed" in result
    assert "approval: pending" in result
    assert "approval_history: 1" in result
    assert "halt_reason: plan_max_iterations_exhausted" in result
    assert "Done" in result


def test_render_trace_dashboard_html_combines_sections() -> None:
    payload: dict[str, Any] = {
        "run_id": "run-123",
        "request": "Find bug",
        "intent": "debug",
        "verification": {"ok": True, "acceptance_ok": False},
        "approval": {
            "pending": True,
            "summary": "apply_patch requires approval",
            "history": [{"decision": "approved", "actor": "chris", "ts": "2026-01-01T00:00:00Z"}],
        },
        "halt_reason": "plan_max_iterations_exhausted",
        "telemetry": {
            "diagnostics": [{"tool": "exec", "summary": "x"}],
            "context_budget": {"working_set": {"token_estimate": 512}},
        },
        "checkpoint": {"thread_id": "thread-a", "latest_checkpoint_id": "cp-1"},
        "events": [{"ts_ms": 1, "kind": "node", "data": {"name": "ingest", "phase": "end"}}],
        "tool_results": [{"tool": "search_files", "ok": True}],
        "final": "Done",
    }
    result = render_trace_dashboard_html(payload, mermaid_graph='flowchart LR\n  A["A"]')
    assert "<!DOCTYPE html>" in result
    assert "Lula Dashboard" in result
    assert "Graph" in result
    assert "Timeline" in result
    assert "Tool Results" in result
    assert "Final Output" in result
    assert "flowchart LR" in result
    assert "verification" in result
    assert "acceptance" in result
    assert "plan_max_iterations_exhausted" in result
    assert "working_set_tokens" in result
    assert "diagnostics" in result
    assert "thread-a" in result
    assert "cp-1" in result
    assert "Approval History" in result
    assert "apply_patch requires approval" in result
    assert "Done" in result


def test_render_run_viewer_spa_returns_html() -> None:
    result = render_run_viewer_spa()
    assert result.startswith("<!DOCTYPE html>")
    assert "<script" in result


def test_render_run_viewer_spa_contains_api_calls() -> None:
    result = render_run_viewer_spa()
    assert "/v1/runs" in result
    assert "approvalAction('approve')" in result
    assert "approvalAction('reject')" in result


def test_render_run_viewer_spa_contains_submit_form() -> None:
    result = render_run_viewer_spa()
    assert "<input" in result


def test_render_run_viewer_spa_custom_api_base_url() -> None:
    result = render_run_viewer_spa(api_base_url="https://api.example.com")
    assert "https://api.example.com" in result


def test_render_run_viewer_spa_diff_css_classes() -> None:
    result = render_run_viewer_spa()
    assert ".diff-add" in result
    assert ".diff-remove" in result
    assert "Approval History" in result


def test_render_run_viewer_spa_bootstraps_bearer_token_and_sse_query_auth() -> None:
    result = render_run_viewer_spa()
    assert "bootstrapBearerToken()" in result
    assert "localStorage.setItem('lgBearerToken'" in result
    assert "accessTokenQuery()" in result
    assert "new EventSource(url)" in result
    assert "/stream' + accessTokenQuery()" in result


def test_render_run_viewer_spa_uses_button_type_for_run_submission() -> None:
    result = render_run_viewer_spa()
    expected = '<button type="button" class="btn btn-primary" onclick="submitRun()">▶ Run</button>'
    assert expected in result


def test_render_run_viewer_spa_contains_no_python_noqa_artifacts() -> None:
    result = render_run_viewer_spa()
    assert "# noqa" not in result


def test_render_trace_site_index_html_lists_runs() -> None:
    result = render_trace_site_index_html(
        [
            {
                "run_id": "abc",
                "request": "Analyze logs",
                "intent": "debug",
                "dashboard_href": "run-abc.html",
                "trace_href": "traces/run-abc.json",
                "events_count": 3,
                "tool_results_count": 1,
                "verification_ok": True,
                "acceptance_ok": False,
                "halt_reason": "plan_max_iterations_exhausted",
                "working_set_tokens": 256,
                "checkpoint_id": "cp-1",
            }
        ]
    )
    assert "<!DOCTYPE html>" in result
    assert "Lula Trace Site" in result
    assert "run-abc.html" in result
    assert "traces/run-abc.json" in result
    assert "Analyze logs" in result
    assert "verify=ok" in result
    assert "accept=fail" in result
    assert "plan_max_iterations_exhausted" in result
    assert "working_set=256" in result
    assert "cp-1" in result
