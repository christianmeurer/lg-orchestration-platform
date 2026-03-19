from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from lg_orch.logging import get_logger
from lg_orch.memory import (
    build_context_layers,
    ensure_history_policy,
    get_compression_summary,
    prune_pre_verification_history,
    record_compression_provenance,
)
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
    raw_request_id = state.get("_request_id")
    request_id = str(raw_request_id).strip() if raw_request_id is not None else None
    return RunnerClient(base_url=base_url, api_key=api_key, request_id=request_id)


def _runner_context_snapshot(
    state: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    import json as _json

    client = _runner_client_from_state(state)
    if client is None:
        return {}, [], ""
    query = _semantic_query_from_request(str(state.get("request", "")))
    try:
        batch_results = client.batch_execute_tools(calls=[
            {"tool": "ast_index_summary", "input": {"max_files": 200}},
            {"tool": "search_codebase", "input": {"query": query, "limit": 8}},
        ])
    finally:
        client.close()

    # Parse ast_index_summary result (index 0)
    ast_map: dict[str, Any] = {}
    if len(batch_results) > 0:
        env0 = batch_results[0]
        if bool(env0.get("ok", False)) is True:
            stdout0 = env0.get("stdout", "")
            if isinstance(stdout0, str) and stdout0.strip():
                try:
                    parsed0 = _json.loads(stdout0)
                    if isinstance(parsed0, dict):
                        ast_map = parsed0
                except _json.JSONDecodeError:
                    pass

    # Parse search_codebase result (index 1)
    semantic_hits: list[dict[str, Any]] = []
    if len(batch_results) > 1:
        env1 = batch_results[1]
        if bool(env1.get("ok", False)) is True:
            stdout1 = env1.get("stdout", "")
            if isinstance(stdout1, str) and stdout1.strip():
                try:
                    parsed1 = _json.loads(stdout1)
                    if isinstance(parsed1, list):
                        semantic_hits = [row for row in parsed1 if isinstance(row, dict)]
                except _json.JSONDecodeError:
                    pass

    return ast_map, semantic_hits, query


def _mcp_catalog_snapshot(
    state: dict[str, Any],
) -> tuple[dict[str, Any], str, list[dict[str, Any]]]:
    """Discover MCP tools and build a summary for context layers.

    Returns:
        (summary_dict, catalog_string, raw_tool_list)

    On any failure returns ({}, "", []) so callers never need to guard.
    """
    if bool(state.get("_mcp_enabled", False)) is not True:
        return {}, "", []

    client = _runner_client_from_state(state)
    if client is None:
        return {}, "", []
    servers_raw = state.get("_mcp_servers", {})
    servers = dict(servers_raw) if isinstance(servers_raw, dict) else {}
    if not servers:
        client.close()
        return {}, "", []

    try:
        mcp = MCPClient(runner_client=client, server_configs=servers)
        raw_tools = mcp.discover_tools()
        summary = mcp.summarize_tools(tools=raw_tools)
    finally:
        client.close()

    # Exclude hash-mismatch sentinel entries from the raw list surfaced to state.
    clean_tools: list[dict[str, Any]] = (
        [t for t in raw_tools if isinstance(t, dict) and not bool(t.get("_schema_hash_mismatch", False))]
        if isinstance(raw_tools, list)
        else []
    )
    return summary, str(summary.get("summary", "")).strip(), clean_tools


def _mcp_recovery_hints(state: dict[str, Any], mcp_summary: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    if not mcp_summary:
        return "", []

    recovery_packet_raw = state.get("recovery_packet", {})
    recovery_packet = dict(recovery_packet_raw) if isinstance(recovery_packet_raw, dict) else {}
    request = str(state.get("request", "")).strip().lower()
    keywords = {token.lower() for token in _WORD_RE.findall(request)}
    failure_class = str(recovery_packet.get("failure_class", "")).strip().lower()
    last_check = str(recovery_packet.get("last_check", "")).strip().lower()
    keywords.update(token.lower() for token in _WORD_RE.findall(failure_class))
    keywords.update(token.lower() for token in _WORD_RE.findall(last_check))

    servers_raw = mcp_summary.get("servers", [])
    servers = [entry for entry in servers_raw if isinstance(entry, dict)] if isinstance(servers_raw, list) else []
    relevant_tools: list[dict[str, Any]] = []
    fallback_tools: list[dict[str, Any]] = []
    for server in servers:
        server_name = str(server.get("server_name", "")).strip() or "unknown"
        tools_raw = server.get("tools", [])
        tools = [entry for entry in tools_raw if isinstance(entry, dict)] if isinstance(tools_raw, list) else []
        for tool in tools:
            name = str(tool.get("name", "")).strip()
            description = str(tool.get("description", "")).strip()
            if not name:
                continue
            entry = {
                "server_name": server_name,
                "name": name,
                "description": description,
            }
            fallback_tools.append(entry)
            haystack = f"{name} {description}".lower()
            if keywords and any(keyword in haystack for keyword in keywords if keyword):
                relevant_tools.append(entry)

    if not relevant_tools:
        relevant_tools = fallback_tools[:5]
    else:
        relevant_tools = relevant_tools[:5]

    lines: list[str] = []
    if recovery_packet:
        lines.append(
            f"recovery_focus: {recovery_packet.get('failure_class', '')} | {recovery_packet.get('last_check', '')}"
        )
    if relevant_tools:
        lines.append(
            "candidate_tools: "
            + ", ".join(
                f"{tool['server_name']}.{tool['name']}" for tool in relevant_tools if tool.get("name")
            )
        )
    return "\n".join(line for line in lines if line.strip()).strip(), relevant_tools


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


def _load_cached_procedures(state: dict[str, Any]) -> list[dict[str, Any]]:
    procedure_cache_path = str(state.get("_procedure_cache_path", "")).strip()
    if not procedure_cache_path:
        return []
    request = str(state.get("request", "")).strip()
    if not request:
        return []
    try:
        from lg_orch.procedure_cache import ProcedureCache
        cache = ProcedureCache(db_path=Path(procedure_cache_path))
        try:
            return cache.lookup_procedure(request=request, limit=3)
        finally:
            cache.close()
    except Exception:
        return []


def _load_episodic_context(state: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Load cross-session recovery facts from the run store if configured.
    Returns empty list if no run store path or on any error.
    """
    run_store_path = str(state.get("_run_store_path", "")).strip()
    if not run_store_path:
        return []
    recovery_packet_raw = state.get("recovery_packet", {})
    recovery_packet = dict(recovery_packet_raw) if isinstance(recovery_packet_raw, dict) else {}
    fingerprint = str(recovery_packet.get("failure_fingerprint", "")).strip()
    failure_class = str(recovery_packet.get("failure_class", "")).strip()
    if not fingerprint and not failure_class:
        return []
    try:
        from lg_orch.run_store import RunStore
        store = RunStore(db_path=Path(run_store_path))
        try:
            return store.get_episodic_context(
                failure_fingerprint=fingerprint,
                failure_class=failure_class,
                limit=5,
            )
        finally:
            store.close()
    except Exception:
        return []


def _load_semantic_context(state: dict[str, Any]) -> list[dict[str, Any]]:
    run_store_path = str(state.get("_run_store_path", "")).strip()
    if not run_store_path:
        return []
    query = _semantic_query_from_request(str(state.get("request", "")))
    if not query:
        return []
    try:
        from lg_orch.run_store import RunStore
        store = RunStore(db_path=Path(run_store_path))
        try:
            return store.search_semantic_memories(query=query, limit=5)
        finally:
            store.close()
    except Exception:
        return []


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

    mcp_tools: list[dict[str, Any]] = []
    try:
        mcp_summary, mcp_catalog, mcp_tools = _mcp_catalog_snapshot(state)
        if mcp_summary:
            repo_context["mcp_capabilities"] = mcp_summary
        if mcp_catalog:
            repo_context["mcp_catalog"] = mcp_catalog
        if mcp_tools:
            tool_names = [
                str(t.get("name", "")).strip()
                for t in mcp_tools
                if isinstance(t, dict) and t.get("name")
            ]
            if tool_names:
                repo_context["mcp_tools_catalog"] = "Available MCP tools: " + ", ".join(tool_names)
        mismatch_servers = mcp_summary.get("mismatch_servers", [])
        if mismatch_servers:
            log.warning(
                "mcp_schema_hash_mismatch_servers_excluded",
                servers=mismatch_servers,
            )
            repo_context["mcp_hash_mismatches"] = mismatch_servers
        mcp_recovery_hints, mcp_relevant_tools = _mcp_recovery_hints(state, mcp_summary)
        if mcp_recovery_hints:
            repo_context["mcp_recovery_hints"] = mcp_recovery_hints
        if mcp_relevant_tools:
            repo_context["mcp_relevant_tools"] = mcp_relevant_tools
    except Exception as exc:
        log.warning("context_builder_mcp_catalog_failed", error=str(exc))
        mcp_tools = []

    # Episodic memory: cross-session recovery facts
    episodic_facts = _load_episodic_context(state)
    if episodic_facts:
        repo_context["episodic_facts"] = episodic_facts

    semantic_memories = _load_semantic_context(state)
    if semantic_memories:
        repo_context["semantic_memories"] = semantic_memories

    # Procedural memory: inject cached procedures for routine operations
    cached_procedures = _load_cached_procedures(state)
    if cached_procedures:
        repo_context["cached_procedures"] = cached_procedures

    layers = build_context_layers(state=state, repo_context=repo_context)
    repo_context["semantic_hits"] = layers["semantic_hits"]
    repo_context["stable_prefix"] = layers["stable_prefix"]
    repo_context["working_set"] = layers["working_set"]
    repo_context["planner_context"] = layers["planner_context"]
    repo_context["compression"] = layers["compression"]
   
    budgets_raw = state.get("budgets", {})
    budgets = dict(budgets_raw) if isinstance(budgets_raw, dict) else {}
    current_loop = int(budgets.get("current_loop", 0) or 0)
   
    out = record_compression_provenance(
        state,
        compression_result=layers,
        current_loop=current_loop,
    )
   
    provenance_raw = out.get("provenance", [])
    provenance = list(provenance_raw) if isinstance(provenance_raw, list) else []
    provenance.append(
        {
            "event": "context_layers_built",
            "stable_prefix_tokens": repo_context["stable_prefix"].get("token_estimate", 0),
            "working_set_tokens": repo_context["working_set"].get("token_estimate", 0),
            "compression": layers["compression"],
        }
    )
   
    telemetry_raw = out.get("telemetry", {})
    telemetry = dict(telemetry_raw) if isinstance(telemetry_raw, dict) else {}
    telemetry["context_budget"] = {
        "stable_prefix": repo_context["stable_prefix"],
        "working_set": repo_context["working_set"],
    }
    telemetry["compression_summary"] = get_compression_summary(out)
   
    out = {**out, "repo_context": repo_context, "provenance": provenance[-20:], "telemetry": telemetry, "mcp_tools": mcp_tools}
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
