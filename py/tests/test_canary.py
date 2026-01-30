from __future__ import annotations

from lg_orch.graph import build_graph


def test_graph_smoke() -> None:
    app = build_graph()
    out = app.invoke(
        {
            "request": "summarize repo",
            "_repo_root": ".",
            "_runner_base_url": "http://127.0.0.1:8088",
            "_runner_enabled": False,
            "_budget_max_loops": 1,
            "_config_policy": {"network_default": "deny", "require_approval_for_mutations": True},
        }
    )
    assert "intent" in out
    assert "final" in out
