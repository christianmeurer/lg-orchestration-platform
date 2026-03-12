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
    with tempfile.TemporaryDirectory() as td:
        mock_client = MagicMock()
        mock_client.get_ast_index_summary.return_value = {
            "schema_version": 1,
            "version": 3,
            "files": [{"path": "py/a.py", "language": "python", "bytes": 10, "symbols": []}],
        }
        mock_client.search_codebase.return_value = [
            {
                "path": "py/a.py",
                "language": "python",
                "symbols": ["alpha"],
                "snippet": "def alpha",
                "score": 0.1,
            }
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
        mock_client.get_ast_index_summary.return_value = {}
        mock_client.search_codebase.return_value = []
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
