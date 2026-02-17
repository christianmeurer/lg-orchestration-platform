from __future__ import annotations

from pathlib import Path
from typing import Any

from lg_orch.logging import get_logger
from lg_orch.trace import append_event


def context_builder(state: dict[str, Any]) -> dict[str, Any]:
    state = append_event(state, kind="node", data={"name": "context_builder", "phase": "start"})
    log = get_logger()
    repo_root = Path(state.get("_repo_root", ".")).resolve()
    repo_context = dict(state.get("repo_context", {}))
    repo_context["repo_root"] = str(repo_root)

    try:
        repo_context["has_py"] = (repo_root / "py").is_dir()
    except OSError:
        repo_context["has_py"] = False

    try:
        repo_context["has_rs"] = (repo_root / "rs").is_dir()
    except OSError:
        repo_context["has_rs"] = False

    try:
        repo_context["top_level"] = sorted(p.name for p in repo_root.iterdir() if p.exists())
    except OSError as exc:
        log.warning("context_builder_iterdir_failed", error=str(exc))
        repo_context["top_level"] = []

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
