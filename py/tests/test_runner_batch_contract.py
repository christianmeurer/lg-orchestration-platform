from __future__ import annotations

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
        assert set(r.keys()) >= {
            "tool",
            "ok",
            "exit_code",
            "stdout",
            "stderr",
            "timing_ms",
            "artifacts",
        }
