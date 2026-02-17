from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

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
