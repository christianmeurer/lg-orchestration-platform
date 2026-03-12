from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from lg_orch.logging import get_logger
from lg_orch.memory import build_context_layers, ensure_history_policy, prune_pre_verification_history
from lg_orch.model_routing import record_model_route
from lg_orch.tools import MCPClient, RunnerClient
from lg_orch.trace import append_event

_WORD_RE = re.compile(r"[a-zA-Z0-9_]+")
_PERSISTENT_REPO_CONTEXT_KEYS = ("system_prompt", "structural_ast_map", "semantic_hits")


def _validate_base_url(url: str) -> bool:
    return url.startswith("http://") or url.startswith("https://")


def _semantic_query_from_request(request: str) -> str:
    tokens = [t.lower() for t in _WORD_RE.findall(request)]
    if not tokens:
        return "repository structure"
    return " ".join(tokens[:8])


def _runner_client_from_state(state: dict[str, Any]) -> RunnerClient | None:
    if bool(state.get("_runner_enabled", True)) is False:
        return None
    raw_url = state.get("_runner_base_url")
    if not isinstance(raw_url, str):
        return None
    base_url = raw_url.strip()
    if not _validate_base_url(base_url):
        return None
    raw_api_key = state.get("_runner_api_key")
    api_key = str(raw_api_key).strip() if raw_api_key is not None else None
    return RunnerClient(base_url=base_url, api_key=api_key)


def _runner_context_snapshot(
    state: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    client = _runner_client_from_state(state)
    if client is None:
        return {}, [], ""
    query = _semantic_query_from_request(str(state.get("request", "")))
    try:
        ast_map = client.get_ast_index_summary(max_files=200)
        semantic_hits = client.search_codebase(query=query, limit=8)
    finally:
        client.close()
    return ast_map, semantic_hits, query


def _mcp_catalog_snapshot(state: dict[str, Any]) -> tuple[dict[str, Any], str]:
    if bool(state.get("_mcp_enabled", False)) is not True:
        return {}, ""

    client = _runner_client_from_state(state)
    if client is None:
        return {}, ""
    servers_raw = state.get("_mcp_servers", {})
    servers = dict(servers_raw) if isinstance(servers_raw, dict) else {}
    if not servers:
        client.close()
        return {}, ""

    try:
        mcp = MCPClient(runner_client=client, server_configs=servers)
        summary = mcp.summarize_tools()
    finally:
        client.close()
    return summary, str(summary.get("summary", "")).strip()


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
    state = ensure_history_policy(state)
    state = record_model_route(
        state,
        node_name="context_builder",
        task_class="summarization",
        model_slot="router",
    )
    state = append_event(state, kind="node", data={"name": "context_builder", "phase": "start"})
    log = get_logger()
    repo_root = Path(state.get("_repo_root", ".")).resolve()
    context_reset_requested = bool(state.get("context_reset_requested", False))
    existing_context_raw = state.get("repo_context", {})
    existing_context = existing_context_raw if isinstance(existing_context_raw, dict) else {}
    repo_context: dict[str, Any] = {} if context_reset_requested else dict(existing_context)
    if context_reset_requested:
        for key in _PERSISTENT_REPO_CONTEXT_KEYS:
            if key in existing_context:
                repo_context[key] = existing_context[key]

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

    try:
        ast_map, semantic_hits, semantic_query = _runner_context_snapshot(state)
        if ast_map:
            repo_context["structural_ast_map"] = ast_map
        if semantic_query:
            repo_context["semantic_query"] = semantic_query
        if semantic_hits:
            repo_context["semantic_hits"] = semantic_hits
    except Exception as exc:
        log.warning("context_builder_runner_context_failed", error=str(exc))

    try:
        mcp_summary, mcp_catalog = _mcp_catalog_snapshot(state)
        if mcp_summary:
            repo_context["mcp_capabilities"] = mcp_summary
        if mcp_catalog:
            repo_context["mcp_catalog"] = mcp_catalog
    except Exception as exc:
        log.warning("context_builder_mcp_catalog_failed", error=str(exc))

    layers = build_context_layers(state=state, repo_context=repo_context)
    repo_context["semantic_hits"] = layers["semantic_hits"]
    repo_context["stable_prefix"] = layers["stable_prefix"]
    repo_context["working_set"] = layers["working_set"]
    repo_context["planner_context"] = layers["planner_context"]
    repo_context["compression"] = layers["compression"]

    provenance_raw = state.get("provenance", [])
    provenance = list(provenance_raw) if isinstance(provenance_raw, list) else []
    provenance.append(
        {
            "event": "context_layers_built",
            "stable_prefix_tokens": repo_context["stable_prefix"].get("token_estimate", 0),
            "working_set_tokens": repo_context["working_set"].get("token_estimate", 0),
            "compression": layers["compression"],
        }
    )

    telemetry_raw = state.get("telemetry", {})
    telemetry = dict(telemetry_raw) if isinstance(telemetry_raw, dict) else {}
    telemetry["context_budget"] = {
        "stable_prefix": repo_context["stable_prefix"],
        "working_set": repo_context["working_set"],
    }

    out = {**state, "repo_context": repo_context, "provenance": provenance, "telemetry": telemetry}
    out = append_event(
        out,
        kind="node",
        data={
            "name": "context_builder",
            "phase": "end",
            "top_level": len(repo_context["top_level"]),
            "planner_context_tokens": repo_context["planner_context"].get("token_estimate", 0),
        },
    )
    return prune_pre_verification_history(out)
