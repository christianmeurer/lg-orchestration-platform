from __future__ import annotations

from pathlib import Path
from typing import Any

from lg_orch.trace import append_event


def context_builder(state: dict[str, Any]) -> dict[str, Any]:
    state = append_event(state, kind="node", data={"name": "context_builder", "phase": "start"})
    repo_root = Path(state.get("_repo_root", ".")).resolve()
    repo_context = dict(state.get("repo_context", {}))
    repo_context["repo_root"] = str(repo_root)
    repo_context["has_py"] = (repo_root / "py").is_dir()
    repo_context["has_rs"] = (repo_root / "rs").is_dir()
    repo_context["top_level"] = sorted(p.name for p in repo_root.iterdir() if p.exists())
    out = {**state, "repo_context": repo_context}
    return append_event(
        out,
        kind="node",
        data={
            "name": "context_builder",
            "phase": "end",
            "top_level": len(repo_context["top_level"]),
        },
    )
