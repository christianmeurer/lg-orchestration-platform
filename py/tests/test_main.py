from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from lg_orch.main import (
    _build_parser,
    _trace_http_response,
    _trace_payload_from_path,
    _trace_run_id,
    _trace_run_summary,
    _validated_run_id,
    cli,
)


def test_parser_accepts_console_view() -> None:
    parser = _build_parser()
    args = parser.parse_args(["run", "hello", "--view", "console"])
    assert args.cmd == "run"
    assert args.view == "console"


def test_parser_accepts_trace_serve() -> None:
    parser = _build_parser()
    args = parser.parse_args(["trace-serve", "runs", "--host", "0.0.0.0", "--port", "8081"])
    assert args.cmd == "trace-serve"
    assert args.host == "0.0.0.0"
    assert args.port == 8081


def test_parser_accepts_serve_api() -> None:
    parser = _build_parser()
    args = parser.parse_args(["serve-api", "--host", "0.0.0.0", "--port", "8082"])
    assert args.cmd == "serve-api"
    assert args.host == "0.0.0.0"
    assert args.port == 8082


def test_parser_accepts_run_trace_correlation_flags() -> None:
    parser = _build_parser()
    args = parser.parse_args(
        ["run", "hello", "--run-id", "run-abc", "--trace-out-dir", "artifacts/api"]
    )
    assert args.cmd == "run"
    assert args.run_id == "run-abc"
    assert args.trace_out_dir == "artifacts/api"


def test_trace_view_renders_dashboard(tmp_path: Path, capsys: object) -> None:
    trace_path = tmp_path / "trace.json"
    payload = {
        "run_id": "abc",
        "request": "Analyze logs",
        "intent": "debug",
        "events": [{"ts_ms": 1, "kind": "ingest", "data": {}}],
        "tool_results": [{"tool": "search_files", "ok": True}],
        "final": "Completed",
    }
    trace_path.write_text(json.dumps(payload), encoding="utf-8")

    rc = cli(["trace-view", str(trace_path), "--width", "72"])
    assert rc == 0

    captured = capsys.readouterr()
    assert "Lula Console" in captured.out
    assert "Timeline" in captured.out
    assert "Tool Results" in captured.out
    assert "Completed" in captured.out


def test_trace_view_writes_html_dashboard(tmp_path: Path) -> None:
    trace_path = tmp_path / "trace.json"
    output_path = tmp_path / "dashboard" / "index.html"
    payload = {
        "run_id": "abc",
        "request": "Analyze logs",
        "intent": "debug",
        "events": [{"ts_ms": 1, "kind": "node", "data": {"name": "ingest", "phase": "end"}}],
        "tool_results": [{"tool": "search_files", "ok": True}],
        "final": "Completed",
    }
    trace_path.write_text(json.dumps(payload), encoding="utf-8")

    rc = cli(["trace-view", str(trace_path), "--format", "html", "--output", str(output_path)])
    assert rc == 0

    rendered = output_path.read_text(encoding="utf-8")
    assert "<!DOCTYPE html>" in rendered
    assert "Lula Dashboard" in rendered
    assert "mermaid" in rendered
    assert "Completed" in rendered


def test_trace_site_writes_index_and_dashboards(tmp_path: Path) -> None:
    trace_dir = tmp_path / "runs"
    trace_dir.mkdir()
    payload_a = {
        "run_id": "abc",
        "request": "Analyze logs",
        "intent": "debug",
        "events": [{"ts_ms": 1, "kind": "node", "data": {"name": "ingest", "phase": "end"}}],
        "tool_results": [{"tool": "search_files", "ok": True}],
        "verification": {"ok": True, "acceptance_ok": False},
        "halt_reason": "plan_max_iterations_exhausted",
        "telemetry": {"context_budget": {"working_set": {"token_estimate": 256}}},
        "checkpoint": {"thread_id": "thread-a", "latest_checkpoint_id": "cp-1"},
        "final": "Completed",
    }
    payload_b = {
        "run_id": "def",
        "request": "Summarize repo",
        "intent": "analysis",
        "events": [],
        "tool_results": [],
        "final": "Done",
    }
    (trace_dir / "run-abc.json").write_text(json.dumps(payload_a), encoding="utf-8")
    (trace_dir / "run-def.json").write_text(json.dumps(payload_b), encoding="utf-8")

    output_dir = tmp_path / "site"
    rc = cli(["trace-site", str(trace_dir), "--output-dir", str(output_dir)])
    assert rc == 0

    index_html = (output_dir / "index.html").read_text(encoding="utf-8")
    assert "Lula Trace Site" in index_html
    assert "run-abc.html" in index_html
    assert "run-def.html" in index_html
    assert "traces/run-abc.json" in index_html
    assert "accept=fail" in index_html
    assert "plan_max_iterations_exhausted" in index_html

    dashboard_html = (output_dir / "run-abc.html").read_text(encoding="utf-8")
    assert "All runs" in dashboard_html
    assert "traces/run-abc.json" in dashboard_html
    assert "Completed" in dashboard_html
    assert (output_dir / "traces" / "run-abc.json").exists()


def test_trace_site_writes_empty_index_for_empty_directory(tmp_path: Path) -> None:
    trace_dir = tmp_path / "runs"
    trace_dir.mkdir()
    output_dir = tmp_path / "site"

    rc = cli(["trace-site", str(trace_dir), "--output-dir", str(output_dir)])
    assert rc == 0

    index_html = (output_dir / "index.html").read_text(encoding="utf-8")
    assert "No runs captured yet." in index_html


def test_trace_http_response_lists_runs_and_renders_dashboard(tmp_path: Path) -> None:
    trace_dir = tmp_path / "runs"
    trace_dir.mkdir()
    payload = {
        "run_id": "abc",
        "request": "Analyze logs",
        "intent": "debug",
        "events": [{"ts_ms": 1, "kind": "node", "data": {"name": "ingest", "phase": "end"}}],
        "tool_results": [{"tool": "search_files", "ok": True}],
        "verification": {"ok": True},
        "checkpoint": {"thread_id": "thread-a", "latest_checkpoint_id": "cp-1"},
        "final": "Completed",
    }
    (trace_dir / "run-abc.json").write_text(json.dumps(payload), encoding="utf-8")

    status, content_type, body = _trace_http_response(
        trace_dir,
        request_path="/v1/runs",
        mermaid_graph="flowchart LR\n  ingest --> reporter",
    )
    assert status == 200
    assert content_type == "application/json; charset=utf-8"
    runs_payload = json.loads(body.decode("utf-8"))
    assert runs_payload["runs"][0]["run_id"] == "abc"
    assert runs_payload["runs"][0]["dashboard_href"] == "/runs/abc"
    assert runs_payload["runs"][0]["verification_ok"] is True
    assert runs_payload["runs"][0]["checkpoint_id"] == "cp-1"

    status, content_type, body = _trace_http_response(
        trace_dir,
        request_path="/runs/abc",
        mermaid_graph="flowchart LR\n  ingest --> reporter",
    )
    assert status == 200
    assert content_type == "text/html; charset=utf-8"
    rendered = body.decode("utf-8")
    assert "Lula Dashboard" in rendered
    assert "All runs" in rendered
    assert "/v1/runs/abc" in rendered


def test_trace_http_response_returns_not_found_for_missing_run(tmp_path: Path) -> None:
    trace_dir = tmp_path / "runs"
    trace_dir.mkdir()

    status, content_type, body = _trace_http_response(
        trace_dir,
        request_path="/v1/runs/missing",
        mermaid_graph="flowchart LR\n  ingest --> reporter",
    )
    assert status == 404
    assert content_type == "application/json; charset=utf-8"
    payload = json.loads(body.decode("utf-8"))
    assert payload["error"] == "not_found"


def test_trace_view_rejects_invalid_json(tmp_path: Path) -> None:
    trace_path = tmp_path / "trace.json"
    trace_path.write_text("{", encoding="utf-8")

    rc = cli(["trace-view", str(trace_path)])
    assert rc == 2


# ---------------------------------------------------------------------------
# Command dispatch structure tests (Item 2)
# ---------------------------------------------------------------------------


def test_cli_trace_view_dispatches_to_trace_command(tmp_path: Path) -> None:
    """cli() must delegate trace-view to commands.trace.trace_view_command."""
    trace_path = tmp_path / "trace.json"
    payload = {"run_id": "x", "request": "r", "final": "done"}
    trace_path.write_text(json.dumps(payload), encoding="utf-8")

    with patch("lg_orch.commands.trace.trace_view_command", return_value=0) as mock_cmd:
        rc = cli(["trace-view", str(trace_path)])
    assert rc == 0
    mock_cmd.assert_called_once()


def test_cli_trace_site_dispatches_to_trace_command(tmp_path: Path) -> None:
    """cli() must delegate trace-site to commands.trace.trace_site_command."""
    trace_dir = tmp_path / "runs"
    trace_dir.mkdir()

    with patch("lg_orch.commands.trace.trace_site_command", return_value=0) as mock_cmd:
        rc = cli(["trace-site", str(trace_dir)])
    assert rc == 0
    mock_cmd.assert_called_once()


def test_cli_serve_api_dispatches_to_serve_command(tmp_path: Path) -> None:
    """cli() must delegate serve-api to commands.serve.serve_command."""
    with patch("lg_orch.commands.serve.serve_command", return_value=0) as mock_cmd:
        rc = cli(["serve-api", "--host", "127.0.0.1", "--port", "9999"])
    assert rc == 0
    mock_cmd.assert_called_once()


def test_cli_run_dispatches_to_run_command(tmp_path: Path) -> None:
    """cli() must delegate run to commands.run.run_command."""
    # Provide a minimal config so load_config does not fail.
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    (cfg_dir / "runtime.dev.toml").write_text(
        """\
[models.router]
provider = "local"
model = "deterministic"
temperature = 0.0

[models.planner]
provider = "local"
model = "deterministic"
temperature = 0.0

[budgets]
max_loops = 1
max_tool_calls_per_loop = 1
max_patch_bytes = 1
tool_timeout_s = 1

[policy]
network_default = "deny"
require_approval_for_mutations = false

[runner]
base_url = "http://localhost:8088"
root_dir = "."

[mcp]
enabled = false
""",
        encoding="utf-8",
    )

    with patch("lg_orch.commands.run.run_command", return_value=0) as mock_cmd:
        rc = cli(["run", "hello world", "--repo-root", str(tmp_path)])
    assert rc == 0
    mock_cmd.assert_called_once()


def test_run_command_callable_directly(tmp_path: Path) -> None:
    """run_command() must be importable and callable as a standalone function."""
    from lg_orch.commands.run import run_command

    assert callable(run_command)


def test_serve_command_callable_directly() -> None:
    """serve_command() must be importable and callable as a standalone function."""
    from lg_orch.commands.serve import serve_command

    assert callable(serve_command)


def test_trace_commands_callable_directly() -> None:
    """trace_view_command / trace_site_command / trace_serve_command importable."""
    from lg_orch.commands.trace import (
        trace_serve_command,
        trace_site_command,
        trace_view_command,
    )

    assert callable(trace_view_command)
    assert callable(trace_site_command)
    assert callable(trace_serve_command)


def test_heal_command_callable_directly() -> None:
    """heal_command() must be importable and callable as a standalone function."""
    from lg_orch.commands.heal import heal_command

    assert callable(heal_command)


# ---------------------------------------------------------------------------
# _validated_run_id
# ---------------------------------------------------------------------------


def test_validated_run_id_returns_none_for_none() -> None:
    assert _validated_run_id(None) is None


def test_validated_run_id_returns_none_for_empty() -> None:
    assert _validated_run_id("") is None
    assert _validated_run_id("   ") is None


def test_validated_run_id_returns_none_for_invalid_chars() -> None:
    assert _validated_run_id("run/../../etc") is None
    assert _validated_run_id("run id with spaces") is None


def test_validated_run_id_accepts_valid_ids() -> None:
    assert _validated_run_id("run-abc") == "run-abc"
    assert _validated_run_id("run_123.test") == "run_123.test"
    assert _validated_run_id("A") == "A"


# ---------------------------------------------------------------------------
# _trace_run_id
# ---------------------------------------------------------------------------


def test_trace_run_id_uses_payload_run_id() -> None:
    p = Path("/tmp/run-fallback.json")
    assert _trace_run_id(p, {"run_id": "from-payload"}) == "from-payload"


def test_trace_run_id_falls_back_to_filename() -> None:
    p = Path("/tmp/run-abc123.json")
    assert _trace_run_id(p, {}) == "abc123"


def test_trace_run_id_strips_whitespace() -> None:
    p = Path("/tmp/run-fallback.json")
    assert _trace_run_id(p, {"run_id": "  trimmed  "}) == "trimmed"


# ---------------------------------------------------------------------------
# _trace_run_summary
# ---------------------------------------------------------------------------


def test_trace_run_summary_builds_complete_structure() -> None:
    p = Path("/tmp/run-abc.json")
    payload = {
        "run_id": "abc",
        "request": "Analyze logs",
        "intent": "debug",
        "events": [{"ts_ms": 1}],
        "tool_results": [{"tool": "search"}],
        "verification": {"ok": True, "acceptance_ok": False},
        "halt_reason": "plan_done",
        "telemetry": {"context_budget": {"working_set": {"token_estimate": 512}}},
        "checkpoint": {"thread_id": "t-1", "latest_checkpoint_id": "cp-1"},
    }
    summary = _trace_run_summary(
        trace_path=p,
        payload=payload,
        dashboard_href="/runs/abc",
        trace_href="/v1/runs/abc",
    )
    assert summary["run_id"] == "abc"
    assert summary["request"] == "Analyze logs"
    assert summary["intent"] == "debug"
    assert summary["events_count"] == 1
    assert summary["tool_results_count"] == 1
    assert summary["verification_ok"] is True
    assert summary["acceptance_ok"] is False
    assert summary["halt_reason"] == "plan_done"
    assert summary["working_set_tokens"] == 512
    assert summary["checkpoint_thread_id"] == "t-1"
    assert summary["checkpoint_id"] == "cp-1"


def test_trace_run_summary_handles_minimal_payload() -> None:
    p = Path("/tmp/run-xyz.json")
    summary = _trace_run_summary(
        trace_path=p,
        payload={},
        dashboard_href="/runs/xyz",
        trace_href="/v1/runs/xyz",
    )
    assert summary["request"] == "(empty request)"
    assert summary["intent"] == "(pending)"
    assert summary["events_count"] == 0
    assert summary["tool_results_count"] == 0


# ---------------------------------------------------------------------------
# _trace_payload_from_path
# ---------------------------------------------------------------------------


def test_trace_payload_from_path_returns_none_for_missing_file(tmp_path: Path) -> None:
    result = _trace_payload_from_path(tmp_path / "nonexistent.json", warn_context="test")
    assert result is None


def test_trace_payload_from_path_returns_none_for_invalid_json(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{broken", encoding="utf-8")
    result = _trace_payload_from_path(bad, warn_context="test")
    assert result is None


def test_trace_payload_from_path_returns_none_for_non_object(tmp_path: Path) -> None:
    arr = tmp_path / "array.json"
    arr.write_text("[1, 2, 3]", encoding="utf-8")
    result = _trace_payload_from_path(arr, warn_context="test")
    assert result is None


def test_trace_payload_from_path_returns_dict_for_valid(tmp_path: Path) -> None:
    good = tmp_path / "good.json"
    good.write_text('{"run_id": "abc"}', encoding="utf-8")
    result = _trace_payload_from_path(good, warn_context="test")
    assert result == {"run_id": "abc"}


# ---------------------------------------------------------------------------
# _trace_http_response — additional routes
# ---------------------------------------------------------------------------


def test_trace_http_response_returns_404_for_unknown_route(tmp_path: Path) -> None:
    trace_dir = tmp_path / "runs"
    trace_dir.mkdir()
    status, _, _body = _trace_http_response(
        trace_dir, request_path="/unknown/path", mermaid_graph=""
    )
    assert status == 404


def test_trace_http_response_index_returns_html(tmp_path: Path) -> None:
    trace_dir = tmp_path / "runs"
    trace_dir.mkdir()
    status, content_type, _body = _trace_http_response(
        trace_dir, request_path="/", mermaid_graph=""
    )
    assert status == 200
    assert "text/html" in content_type


def test_trace_http_response_v1_run_returns_json(tmp_path: Path) -> None:
    trace_dir = tmp_path / "runs"
    trace_dir.mkdir()
    payload = {"run_id": "test1", "request": "hello"}
    (trace_dir / "run-test1.json").write_text(json.dumps(payload), encoding="utf-8")
    status, content_type, body = _trace_http_response(
        trace_dir, request_path="/v1/runs/test1", mermaid_graph=""
    )
    assert status == 200
    assert "application/json" in content_type
    data = json.loads(body.decode("utf-8"))
    assert data["run_id"] == "test1"


def test_trace_http_response_runs_dashboard_missing_returns_404(tmp_path: Path) -> None:
    trace_dir = tmp_path / "runs"
    trace_dir.mkdir()
    status, _, _ = _trace_http_response(
        trace_dir, request_path="/runs/nonexistent", mermaid_graph=""
    )
    assert status == 404
