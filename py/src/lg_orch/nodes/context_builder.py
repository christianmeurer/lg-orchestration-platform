from __future__ import annotations

from pathlib import Path
from typing import Any

from lg_orch.logging import get_logger
from lg_orch.trace import append_event


def _generate_repo_map(root: Path, max_depth: int = 3) -> str:
    """Generates a tree-like repo map, respecting a max depth to prevent huge context."""
    lines = []

    def _walk(dir_path: Path, prefix: str = "", current_depth: int = 0) -> None:
        if current_depth > max_depth:
            return

        try:
            paths = sorted(
                p for p in dir_path.iterdir() if p.exists() and not p.name.startswith(".")
            )
        except OSError:
            return

        for i, p in enumerate(paths):
            is_last = i == len(paths) - 1
            connector = "└── " if is_last else "├── "
            lines.append(f"{prefix}{connector}{p.name}")
            if p.is_dir():
                new_prefix = prefix + ("    " if is_last else "│   ")
                _walk(p, new_prefix, current_depth + 1)

    _walk(root)
    return "\n".join(lines)


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

    # Generate an intelligent repo map for SOTA agentic context
    try:
        repo_context["repo_map"] = _generate_repo_map(repo_root)
    except Exception as exc:
        log.warning("context_builder_repo_map_failed", error=str(exc))
        repo_context["repo_map"] = ""

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
