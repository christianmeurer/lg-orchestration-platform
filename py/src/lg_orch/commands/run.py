# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Christian Meurer — https://github.com/christianmeurer/Lula
"""run_command — core graph invocation, checkpoint resumption, output reporting.

Extracted from ``lg_orch.main.cli`` so the monolithic CLI dispatcher stays
under 200 lines.  All heavy state-construction logic lives here.
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from typing import Any, cast

from langchain_core.runnables.config import RunnableConfig

from lg_orch.checkpointing import (
    create_checkpoint_saver,
    resolve_checkpoint_db_path,
    stable_checkpoint_thread_id,
)
from lg_orch.config import AppConfig
from lg_orch.graph import build_graph
from lg_orch.logging import get_logger
from lg_orch.trace import write_run_trace
from lg_orch.visualize import render_run_header, render_trace_dashboard


def run_command(args: Any, *, cfg: AppConfig, repo_root: Path) -> int:
    """Execute the main orchestration graph for a single request.

    Parameters
    ----------
    args:
        Parsed argparse namespace from the ``run`` subcommand.
    cfg:
        Already-loaded :class:`~lg_orch.config.AppConfig`.
    repo_root:
        Resolved repository root path.
    """
    import os

    log = get_logger()

    from lg_orch.main import _validated_run_id  # thin helper stays in main

    provided_run_id = _validated_run_id(getattr(args, "run_id", None))
    if getattr(args, "run_id", None) and provided_run_id is None:
        log.error("run_id_invalid", run_id=str(args.run_id))
        return 2

    runner_base_url = args.runner_base_url or cfg.runner.base_url
    trace_enabled = bool(args.trace) or cfg.trace.enabled
    trace_out_dir_raw = getattr(args, "trace_out_dir", None)
    trace_out_dir = str(trace_out_dir_raw).strip() if trace_out_dir_raw is not None else ""

    resume_approvals: dict[str, Any] | None = None
    resume_approvals_raw = str(os.environ.get("LG_RESUME_APPROVALS_JSON", "")).strip()
    if resume_approvals_raw:
        try:
            parsed_resume_approvals = json.loads(resume_approvals_raw)
        except json.JSONDecodeError as exc:
            log.warning("resume_approvals_json_invalid", error=str(exc))
        else:
            if isinstance(parsed_resume_approvals, dict):
                resume_approvals = parsed_resume_approvals

    approval_context: dict[str, Any] | None = None
    approval_context_raw = str(os.environ.get("LG_APPROVAL_CONTEXT_JSON", "")).strip()
    if approval_context_raw:
        try:
            parsed_approval_context = json.loads(approval_context_raw)
        except json.JSONDecodeError as exc:
            log.warning("approval_context_json_invalid", error=str(exc))
        else:
            if isinstance(parsed_approval_context, dict):
                approval_context = parsed_approval_context

    request_id = str(os.environ.get("LG_REQUEST_ID", "")).strip()
    remote_api_auth_subject = str(os.environ.get("LG_REMOTE_API_AUTH_SUBJECT", "")).strip()
    remote_api_client_ip = str(os.environ.get("LG_REMOTE_API_CLIENT_IP", "")).strip()

    checkpointer = None
    checkpoint_runtime: dict[str, str | bool] = {
        "enabled": bool(cfg.checkpoint.enabled),
        "db_path": "",
        "thread_id": "",
        "checkpoint_ns": cfg.checkpoint.namespace,
        "resume": bool(args.resume),
        "resume_checkpoint_id": "",
    }
    run_config: dict[str, Any] | None = None
    if cfg.checkpoint.enabled:
        db_path = resolve_checkpoint_db_path(repo_root=repo_root, db_path=cfg.checkpoint.db_path)
        _backend = cfg.checkpoint.backend
        if _backend == "redis":
            checkpointer = create_checkpoint_saver(
                "redis",
                redis_url=cfg.checkpoint.redis_url,
                ttl_seconds=cfg.checkpoint.redis_ttl_seconds,
            )
        elif _backend == "postgres":
            checkpointer = create_checkpoint_saver(
                "postgres",
                dsn=cfg.checkpoint.postgres_dsn,
            )
        else:
            checkpointer = create_checkpoint_saver("sqlite", db_path=db_path)
        thread_id = stable_checkpoint_thread_id(
            request=str(args.request),
            thread_prefix=cfg.checkpoint.thread_prefix,
            provided=getattr(args, "thread_id", None),
        )

        checkpoint_runtime = {
            "enabled": True,
            "db_path": str(db_path),
            "thread_id": thread_id,
            "checkpoint_ns": cfg.checkpoint.namespace,
            "resume": bool(args.resume),
            "resume_checkpoint_id": "",
        }

        configurable: dict[str, str] = {
            "thread_id": thread_id,
            "checkpoint_ns": cfg.checkpoint.namespace,
        }
        explicit_checkpoint_id = ""
        if getattr(args, "checkpoint_id", None):
            explicit_checkpoint_id = str(args.checkpoint_id).strip()
        if explicit_checkpoint_id:
            configurable["checkpoint_id"] = explicit_checkpoint_id
            checkpoint_runtime["resume_checkpoint_id"] = explicit_checkpoint_id
        elif bool(args.resume):
            latest_tuple = checkpointer.get_tuple(
                {
                    "configurable": {
                        "thread_id": thread_id,
                        "checkpoint_ns": cfg.checkpoint.namespace,
                    }
                }
            )
            if latest_tuple is not None:
                latest_cfg = latest_tuple.config.get("configurable", {})
                if isinstance(latest_cfg, dict):
                    latest_checkpoint_id = latest_cfg.get("checkpoint_id")
                    if isinstance(latest_checkpoint_id, str) and latest_checkpoint_id.strip():
                        configurable["checkpoint_id"] = latest_checkpoint_id.strip()
                        checkpoint_runtime["resume_checkpoint_id"] = latest_checkpoint_id.strip()
        run_config = {"configurable": configurable}

    app = build_graph(checkpointer=checkpointer)
    run_id = provided_run_id or uuid.uuid4().hex
    state: dict[str, Any] = {
        "request": str(args.request),
        "_run_id": run_id,
        "_repo_root": str(repo_root),
        "_runner_base_url": runner_base_url,
        "_runner_api_key": cfg.runner.api_key,
        "_mcp_enabled": cfg.mcp.enabled,
        "_mcp_servers": {
            name: {
                "command": server.command,
                "args": list(server.args),
                **({"cwd": server.cwd} if server.cwd is not None else {}),
                "env": dict(server.env),
                "timeout_s": server.timeout_s,
            }
            for name, server in cfg.mcp.servers.items()
        },
        "_checkpoint": checkpoint_runtime,
        "_models": {
            "router": {
                "provider": cfg.models.router.provider,
                "model": cfg.models.router.model,
                "temperature": cfg.models.router.temperature,
            },
            "planner": {
                "provider": cfg.models.planner.provider,
                "model": cfg.models.planner.model,
                "temperature": cfg.models.planner.temperature,
            },
        },
        "_model_routing_policy": {
            "local_provider": cfg.models.routing.local_provider,
            "fallback_task_classes": list(cfg.models.routing.fallback_task_classes),
            "interactive_context_limit": cfg.models.routing.interactive_context_limit,
            "deep_planning_context_limit": cfg.models.routing.deep_planning_context_limit,
            "recovery_retry_threshold": cfg.models.routing.recovery_retry_threshold,
            "default_cache_affinity": cfg.models.routing.default_cache_affinity,
        },
        "_model_provider_runtime": {
            "digitalocean": {
                "base_url": cfg.models.digitalocean.base_url,
                "api_key": cfg.models.digitalocean.api_key,
                "timeout_s": cfg.models.digitalocean.timeout_s,
            },
            "openai_compatible": {
                "base_url": cfg.models.openai_compatible.base_url,
                "api_key": cfg.models.openai_compatible.api_key,
                "timeout_s": cfg.models.openai_compatible.timeout_s,
            },
        },
        "_budget_max_loops": cfg.budgets.max_loops,
        "_budget_max_tool_calls_per_loop": cfg.budgets.max_tool_calls_per_loop,
        "_budget_max_patch_bytes": cfg.budgets.max_patch_bytes,
        "_budget_context": {
            "stable_prefix_tokens": cfg.budgets.stable_prefix_tokens,
            "working_set_tokens": cfg.budgets.working_set_tokens,
            "tool_result_summary_chars": cfg.budgets.tool_result_summary_chars,
        },
        "_config_policy": {
            "network_default": cfg.policy.network_default,
            "require_approval_for_mutations": cfg.policy.require_approval_for_mutations,
        },
        "_trace_enabled": trace_enabled,
        "_trace_out_dir": trace_out_dir or cfg.trace.output_dir,
        "_trace_capture_model_metadata": cfg.trace.capture_model_metadata,
        "_run_store_path": cfg.remote_api.run_store_path or "",
        "_procedure_cache_path": cfg.remote_api.procedure_cache_path or "",
        "_vericoding_enabled": cfg.vericoding.enabled,
        "_vericoding_extensions": list(cfg.vericoding.extensions),
    }
    if request_id:
        state["_request_id"] = request_id
    if remote_api_auth_subject or remote_api_client_ip:
        state["_remote_api_context"] = {
            "auth_subject": remote_api_auth_subject,
            "client_ip": remote_api_client_ip,
        }
    if resume_approvals is not None:
        state["_resume_approvals"] = resume_approvals
    if approval_context is not None:
        state["_approval_context"] = approval_context

    out: dict[str, Any] = dict(state)
    view = str(getattr(args, "view", "classic"))
    if view == "console":
        sys.stdout.write(render_run_header(request=str(args.request), intent=None))
        sys.stdout.write("\n")
    else:
        print("\n--- Starting Lula Platform Agent ---")

    stream_kwargs: dict[str, object] = {"stream_mode": "updates"}
    if run_config is not None:
        stream_kwargs["config"] = run_config

    stream_step = 0
    for event in app.stream(state, **stream_kwargs):
        for node_name, node_state in event.items():
            stream_step += 1
            if view == "console":
                event_count = len(list(node_state.get("_trace_events", [])))
                tool_count = len(list(node_state.get("tool_results", [])))
                print(
                    f"[{stream_step:02d}] node={node_name:<12} "
                    f"events={event_count:<3} tools={tool_count:<3}"
                )
            else:
                print(f"\n[Node: {node_name}]")
                if node_name == "planner":
                    plan = node_state.get("plan", {})
                    steps = plan.get("steps", [])
                    print(f"Generated Plan with {len(steps)} steps.")
                    for s in steps:
                        print(f" - {s.get('id')}: {s.get('description')}")
                elif node_name == "executor":
                    tool_results = node_state.get("tool_results", [])
                    if tool_results:
                        last_result = tool_results[-1]
                        print(
                            f"Executed tool: {last_result.get('tool')} "
                            f"(ok: {last_result.get('ok')})"
                        )
                elif node_name == "verifier":
                    report = node_state.get("verification", {})
                    print(f"Verification ok: {report.get('ok')}")
                elif node_name == "reporter":
                    print("Final output generated.")
            out.update(node_state)

    if run_config is not None and checkpointer is not None:
        latest = checkpointer.get_tuple(cast(RunnableConfig, run_config))
        latest_checkpoint_id = ""
        if latest is not None:
            cfg_inner = latest.config.get("configurable", {})
            if isinstance(cfg_inner, dict):
                raw_checkpoint_id = cfg_inner.get("checkpoint_id")
                if isinstance(raw_checkpoint_id, str):
                    latest_checkpoint_id = raw_checkpoint_id

        cp_meta_raw = out.get("_checkpoint", {})
        cp_meta = dict(cp_meta_raw) if isinstance(cp_meta_raw, dict) else {}
        cp_meta["thread_id"] = str(run_config["configurable"]["thread_id"])
        cp_meta["checkpoint_ns"] = str(run_config["configurable"]["checkpoint_ns"])
        cp_meta["latest_checkpoint_id"] = latest_checkpoint_id
        cp_meta["run_id"] = run_id
        out["_checkpoint"] = cp_meta

    if view == "console":
        trace_payload: dict[str, Any] = {
            "request": str(args.request),
            "intent": out.get("intent"),
            "events": list(out.get("_trace_events", [])),
            "tool_results": list(out.get("tool_results", [])),
            "final": out.get("final", ""),
        }
        sys.stdout.write("\n")
        sys.stdout.write(render_trace_dashboard(trace_payload))
    else:
        print("\n--- Final Output ---")
        sys.stdout.write(str(out.get("final", "")) + "\n")

    log.info(
        "run_complete",
        intent=out.get("intent"),
        runner_enabled=bool(out.get("_runner_enabled", True)),
        trace_enabled=bool(out.get("_trace_enabled", False)),
        tool_results=len(list(out.get("tool_results", []))),
    )

    if bool(out.get("_trace_enabled", False)) is True:
        try:
            trace_path = write_run_trace(
                repo_root=repo_root,
                out_dir=Path(str(out.get("_trace_out_dir", "artifacts/runs"))),
                state=out,
            )
            log.info("trace_written", path=str(trace_path))

            store_path = out.get("_run_store_path")
            if store_path:
                from datetime import UTC, datetime

                from lg_orch.run_store import RunStore

                run_store = RunStore(db_path=repo_root / store_path)
                status = (
                    "failed"
                    if (out.get("recovery_packet") or {}).get("failure_class")
                    else "succeeded"
                )
                if "final" not in out:
                    status = "failed"

                now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
                remote_api_context = out.get("_remote_api_context", {})
                if not isinstance(remote_api_context, dict):
                    remote_api_context = {}

                record = {
                    "run_id": str(out.get("_run_id", "")),
                    "request": str(out.get("request", "")),
                    "status": status,
                    "created_at": now,
                    "started_at": now,
                    "finished_at": now,
                    "exit_code": 0 if status == "succeeded" else 1,
                    "trace_out_dir": str(out.get("_trace_out_dir", "artifacts/runs")),
                    "trace_path": str(trace_path),
                    "request_id": str(out.get("_request_id", "")),
                    "auth_subject": str(remote_api_context.get("auth_subject", "")),
                    "client_ip": str(remote_api_context.get("client_ip", "")),
                }
                run_store.upsert(record)

                facts_raw = out.get("facts", [])
                facts = facts_raw if isinstance(facts_raw, list) else []
                if facts:
                    run_store.upsert_recovery_facts(str(out.get("_run_id", "")), facts)

                run_store.close()

        except OSError as exc:
            log.warning("trace_write_failed", error=str(exc))
    return 0
