from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from lg_orch.nodes.context_builder import context_builder


def _base_state(repo_root: str = ".", **overrides: Any) -> dict[str, Any]:
    s: dict[str, Any] = {
        "request": "test",
        "repo_context": {},
        "_repo_root": repo_root,
    }
    s.update(overrides)
    return s


def test_context_builder_populates_repo_root() -> None:
    with tempfile.TemporaryDirectory() as td:
        out = context_builder(_base_state(repo_root=td))
        assert out["repo_context"]["repo_root"] == str(Path(td).resolve())


def test_context_builder_detects_py_dir() -> None:
    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "py").mkdir()
        out = context_builder(_base_state(repo_root=td))
        assert out["repo_context"]["has_py"] is True


def test_context_builder_detects_rs_dir() -> None:
    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "rs").mkdir()
        out = context_builder(_base_state(repo_root=td))
        assert out["repo_context"]["has_rs"] is True


def test_context_builder_no_py_no_rs() -> None:
    with tempfile.TemporaryDirectory() as td:
        out = context_builder(_base_state(repo_root=td))
        assert out["repo_context"]["has_py"] is False
        assert out["repo_context"]["has_rs"] is False


def test_context_builder_top_level_sorted() -> None:
    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "zzz").mkdir()
        (Path(td) / "aaa").mkdir()
        (Path(td) / "mmm.txt").write_text("hi")
        out = context_builder(_base_state(repo_root=td))
        top = out["repo_context"]["top_level"]
        assert top == sorted(top)
        assert "aaa" in top
        assert "zzz" in top
        assert "mmm.txt" in top


def test_context_builder_empty_dir() -> None:
    with tempfile.TemporaryDirectory() as td:
        out = context_builder(_base_state(repo_root=td))
        assert out["repo_context"]["top_level"] == []


def test_context_builder_creates_trace_events() -> None:
    with tempfile.TemporaryDirectory() as td:
        out = context_builder(_base_state(repo_root=td))
        events = out.get("_trace_events", [])
        names = [e["data"]["name"] for e in events if e["kind"] == "node"]
        assert "context_builder" in names


def test_context_builder_trace_includes_count() -> None:
    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "a").mkdir()
        (Path(td) / "b").mkdir()
        out = context_builder(_base_state(repo_root=td))
        events = out.get("_trace_events", [])
        end_evt = [
            e
            for e in events
            if e["kind"] == "node"
            and e["data"].get("phase") == "end"
            and e["data"].get("name") == "context_builder"
        ]
        assert len(end_evt) == 1
        assert end_evt[0]["data"]["top_level"] == 2


def test_context_builder_resets_context_when_requested() -> None:
    with tempfile.TemporaryDirectory() as td:
        out = context_builder(
            _base_state(
                repo_root=td,
                context_reset_requested=True,
                repo_context={"stale": "value", "repo_root": "/old"},
            )
        )
        repo_context = out["repo_context"]
        assert "stale" not in repo_context
        assert repo_context["repo_root"] == str(Path(td).resolve())


@patch("lg_orch.nodes.context_builder.RunnerClient")
def test_context_builder_fetches_structural_ast_and_semantic_hits(
    mock_client_cls: MagicMock,
) -> None:
    import json as _json

    ast_payload = {
        "schema_version": 1,
        "version": 3,
        "files": [{"path": "py/a.py", "language": "python", "bytes": 10, "symbols": []}],
    }
    semantic_payload = [
        {
            "path": "py/a.py",
            "language": "python",
            "symbols": ["alpha"],
            "snippet": "def alpha",
            "score": 0.1,
        }
    ]
    with tempfile.TemporaryDirectory() as td:
        mock_client = MagicMock()
        mock_client.batch_execute_tools.return_value = [
            {"ok": True, "stdout": _json.dumps(ast_payload), "exit_code": 0, "stderr": "", "diagnostics": [], "timing_ms": 0, "artifacts": {}},
            {"ok": True, "stdout": _json.dumps(semantic_payload), "exit_code": 0, "stderr": "", "diagnostics": [], "timing_ms": 0, "artifacts": {}},
        ]
        mock_client_cls.return_value = mock_client

        out = context_builder(
            _base_state(
                repo_root=td,
                _runner_enabled=True,
                _runner_base_url="http://127.0.0.1:8088",
                _runner_api_key=None,
                request="find alpha context",
            )
        )
        repo_context = out["repo_context"]
        assert repo_context["structural_ast_map"]["schema_version"] == 1
        assert repo_context["semantic_hits"][0]["path"] == "py/a.py"
        assert repo_context["semantic_query"] == "find alpha context"


@patch("lg_orch.nodes.context_builder.RunnerClient")
def test_context_builder_preserves_ast_context_on_reset(mock_client_cls: MagicMock) -> None:
    with tempfile.TemporaryDirectory() as td:
        mock_client = MagicMock()
        mock_client.batch_execute_tools.return_value = [
            {"ok": False, "stdout": "", "exit_code": 1, "stderr": "", "diagnostics": [], "timing_ms": 0, "artifacts": {}},
            {"ok": False, "stdout": "", "exit_code": 1, "stderr": "", "diagnostics": [], "timing_ms": 0, "artifacts": {}},
        ]
        mock_client_cls.return_value = mock_client

        prior_ast = {
            "schema_version": 1,
            "version": 7,
            "files": [{"path": "rs/lib.rs", "language": "rust", "bytes": 20, "symbols": []}],
        }
        out = context_builder(
            _base_state(
                repo_root=td,
                context_reset_requested=True,
                repo_context={
                    "stale": "value",
                    "structural_ast_map": prior_ast,
                    "semantic_hits": [{"path": "rs/lib.rs"}],
                    "system_prompt": "persist",
                },
            )
        )
        repo_context = out["repo_context"]
        assert "stale" not in repo_context
        assert repo_context["structural_ast_map"] == prior_ast
        assert repo_context["semantic_hits"] == [{"path": "rs/lib.rs"}]
        assert repo_context["system_prompt"] == "persist"


def test_context_builder_records_model_routing_telemetry() -> None:
    with tempfile.TemporaryDirectory() as td:
        out = context_builder(
            _base_state(
                repo_root=td,
                telemetry={},
                _models={
                    "router": {
                        "provider": "remote_openai",
                        "model": "gpt-4.1",
                        "temperature": 0.0,
                    }
                },
                _model_routing_policy={
                    "local_provider": "local",
                    "fallback_task_classes": ["summarization"],
                },
            )
        )
        routes = out.get("telemetry", {}).get("model_routing", [])
        assert len(routes) >= 1
        assert routes[-1]["node"] == "context_builder"
        assert routes[-1]["task_class"] == "summarization"


@patch("lg_orch.nodes.context_builder.MCPClient")
def test_context_builder_records_mcp_recovery_hints(mock_mcp_cls: MagicMock) -> None:
    with tempfile.TemporaryDirectory() as td:
        mock_mcp = MagicMock()
        mock_mcp.summarize_tools.return_value = {
            "server_count": 1,
            "tool_count": 2,
            "servers": [
                {
                    "server_name": "mock",
                    "tool_count": 2,
                    "tools": [
                        {"name": "echo", "description": "Echoes text back to the caller."},
                        {"name": "search", "description": "Searches diagnostic traces."},
                    ],
                }
            ],
            "summary": "mock: echo, search",
        }
        mock_mcp_cls.return_value = mock_mcp

        out = context_builder(
            _base_state(
                repo_root=td,
                _mcp_enabled=True,
                _mcp_servers={"mock": {"command": "python", "args": ["server.py"]}},
                _runner_base_url="http://127.0.0.1:8088",
                recovery_packet={
                    "failure_class": "verification_failed",
                    "last_check": "search diagnostic traces",
                },
                request="search diagnostic traces",
            )
        )
        repo_context = out["repo_context"]
        assert "mcp_recovery_hints" in repo_context
        assert "candidate_tools:" in repo_context["mcp_recovery_hints"]
        assert repo_context["mcp_relevant_tools"][0]["name"] in {"echo", "search"}


def test_context_builder_loads_episodic_facts(tmp_path: Path) -> None:
    from lg_orch.run_store import RunStore

    db_path = tmp_path / "runs.sqlite"
    store = RunStore(db_path=db_path)
    try:
        store.upsert_recovery_facts(
            "run-seed",
            [
                {
                    "failure_fingerprint": "fp-abc",
                    "failure_class": "lint",
                    "summary": "ruff E501 violation",
                    "loop": 2,
                    "salience": 7,
                }
            ],
        )
    finally:
        store.close()

    with tempfile.TemporaryDirectory() as td:
        out = context_builder(
            _base_state(
                repo_root=td,
                _run_store_path=str(db_path),
                recovery_packet={
                    "failure_fingerprint": "fp-abc",
                    "failure_class": "lint",
                },
            )
        )
    repo_context = out["repo_context"]
    assert "episodic_facts" in repo_context
    assert len(repo_context["episodic_facts"]) >= 1
    assert repo_context["episodic_facts"][0]["fingerprint"] == "fp-abc"


def test_context_builder_loads_cached_procedures(tmp_path: Any) -> None:
    from lg_orch.procedure_cache import ProcedureCache

    db_path = tmp_path / "procedures.sqlite"
    cache = ProcedureCache(db_path=db_path)
    steps = [{"id": "s1", "tools": [{"tool": "run_tests"}, {"tool": "check_output"}]}]
    cache.store_procedure(
        canonical_name="run_tests_check_output",
        request="run the tests and verify",
        task_class="testing",
        steps=steps,
        verification=[],
        created_at="2026-01-01T00:00:00Z",
    )
    cache.close()

    with tempfile.TemporaryDirectory() as td:
        out = context_builder(
            _base_state(
                repo_root=td,
                request="run the tests and verify",
                _procedure_cache_path=str(db_path),
            )
        )
    repo_context = out["repo_context"]
    assert "cached_procedures" in repo_context
    assert len(repo_context["cached_procedures"]) >= 1
    assert repo_context["cached_procedures"][0]["canonical_name"] == "run_tests_check_output"
