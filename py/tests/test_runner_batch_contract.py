from __future__ import annotations

from typing import Any

from lg_orch.tools.runner_client import RunnerClient


def test_batch_execute_shapes_when_runner_unavailable() -> None:
    client = RunnerClient(base_url="http://127.0.0.1:0")
    results = client.batch_execute_tools(
        calls=[
            {"tool": "list_files", "input": {"path": ".", "recursive": False}},
            {"tool": "read_file", "input": {"path": "README.md"}},
        ]
    )
    assert len(results) == 2
    for r in results:
        assert r["artifacts"]["error"] in {"runner_unavailable", "runner_http_error"}
        assert set(r.keys()) >= {
            "tool",
            "ok",
            "exit_code",
            "stdout",
            "stderr",
            "diagnostics",
            "timing_ms",
            "artifacts",
        }


def test_single_execute_contract_for_new_phase2_tools_when_runner_unavailable() -> None:
    client = RunnerClient(base_url="http://127.0.0.1:0")
    calls: list[tuple[str, dict[str, Any]]] = [
        ("ast_index_summary", {"max_files": 20}),
        ("search_codebase", {"query": "memory context", "limit": 5}),
    ]
    for tool_name, input_payload in calls:
        result = client.execute_tool(tool=tool_name, input=input_payload)
        assert result["tool"] == tool_name
        assert result["ok"] is False
        assert result["exit_code"] == 1
        assert set(result.keys()) >= {
            "tool",
            "ok",
            "exit_code",
            "stdout",
            "stderr",
            "diagnostics",
            "timing_ms",
            "artifacts",
        }
