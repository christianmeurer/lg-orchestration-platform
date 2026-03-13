from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from langchain_core.runnables.config import RunnableConfig

from lg_orch.checkpointing import (
    SqliteCheckpointSaver,
    resolve_checkpoint_db_path,
    stable_checkpoint_thread_id,
)
from lg_orch.config import load_config
from lg_orch.graph import build_graph, export_mermaid
from lg_orch.logging import configure_logging, get_logger
from lg_orch.trace import write_run_trace
from lg_orch.visualize import (
    render_run_header,
    render_trace_dashboard,
    render_trace_dashboard_html,
    render_trace_site_index_html,
)

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _validated_run_id(value: str | None) -> str | None:
    if value is None:
        return None
    normalized_value = value.strip()
    if not normalized_value:
        return None
    if not _RUN_ID_RE.fullmatch(normalized_value):
        return None
    return normalized_value


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="lg-orch")
    sub = p.add_subparsers(dest="cmd", required=True)

    run_p = sub.add_parser("run")
    run_p.add_argument("request")
    run_p.add_argument("--profile", default=None)
    run_p.add_argument("--repo-root", default=None)
    run_p.add_argument("--runner-base-url", default=None)
    run_p.add_argument("--trace", action="store_true")
    run_p.add_argument("--run-id", default=None)
    run_p.add_argument("--trace-out-dir", default=None)
    run_p.add_argument("--resume", action="store_true")
    run_p.add_argument("--thread-id", default=None)
    run_p.add_argument("--checkpoint-id", default=None)
    run_p.add_argument("--view", choices=["classic", "console"], default="console")

    run_multi_p = sub.add_parser("run-multi")
    run_multi_p.add_argument("request")
    run_multi_p.add_argument("--repos", nargs="+", required=True, help="List of repository paths")
    run_multi_p.add_argument("--profile", default=None)
    run_multi_p.add_argument("--runner-base-url", default=None)
    run_multi_p.add_argument("--trace", action="store_true")
    run_multi_p.add_argument("--run-id", default=None)

    sub.add_parser("export-graph")
    trace_view_p = sub.add_parser("trace-view")
    trace_view_p.add_argument("trace_path")
    trace_view_p.add_argument("--width", type=int, default=88)
    trace_view_p.add_argument("--format", choices=["console", "html"], default="console")
    trace_view_p.add_argument("--output", default=None)
    trace_site_p = sub.add_parser("trace-site")
    trace_site_p.add_argument("trace_dir")
    trace_site_p.add_argument("--output-dir", default=None)
    trace_serve_p = sub.add_parser("trace-serve")
    trace_serve_p.add_argument("trace_dir")
    trace_serve_p.add_argument("--host", default="127.0.0.1")
    trace_serve_p.add_argument("--port", type=int, default=8000)
    serve_api_p = sub.add_parser("serve-api")
    serve_api_p.add_argument("--host", default="127.0.0.1")
    serve_api_p.add_argument("--port", type=int, default=8001)
    return p


def _resolve_repo_root(*, repo_root_arg: str | None) -> Path:
    import os

    if repo_root_arg and repo_root_arg.strip():
        return Path(repo_root_arg).expanduser().resolve()
    env_root = os.environ.get("LG_REPO_ROOT")
    if env_root and env_root.strip():
        return Path(env_root).expanduser().resolve()

    def find_root(start: Path) -> Path | None:
        cur = start
        for _ in range(32):
            cfg_dir = cur / "configs"
            if cfg_dir.is_dir():
                try:
                    if any(
                        p.name.startswith("runtime.") and p.suffix == ".toml"
                        for p in cfg_dir.iterdir()
                    ):
                        return cur
                except OSError:
                    pass
            if cur.parent == cur:
                break
            cur = cur.parent
        return None

    cwd = Path.cwd().resolve()
    found = find_root(cwd)
    if found is not None:
        return found

    found = find_root(Path(__file__).resolve().parent)
    if found is not None:
        return found

    return cwd


def _trace_payload_from_path(trace_path: Path, *, warn_context: str) -> dict[str, Any] | None:
    log = get_logger()
    try:
        payload_raw = json.loads(trace_path.read_text(encoding="utf-8"))
    except OSError as exc:
        log.warning(f"{warn_context}_read_failed", path=str(trace_path), error=str(exc))
        return None
    except json.JSONDecodeError as exc:
        log.warning(f"{warn_context}_parse_failed", path=str(trace_path), error=str(exc))
        return None

    if not isinstance(payload_raw, dict):
        log.warning(f"{warn_context}_payload_invalid", path=str(trace_path), expected="object")
        return None
    return payload_raw


def _trace_run_id(trace_path: Path, payload: dict[str, Any]) -> str:
    run_id_raw = payload.get("run_id")
    if isinstance(run_id_raw, str) and run_id_raw.strip():
        return run_id_raw.strip()
    return trace_path.stem.removeprefix("run-")


def _trace_run_summary(
    *,
    trace_path: Path,
    payload: dict[str, Any],
    dashboard_href: str,
    trace_href: str,
) -> dict[str, Any]:
    request = str(payload.get("request", "")).strip() or "(empty request)"
    intent_raw = payload.get("intent")
    intent = str(intent_raw).strip() if isinstance(intent_raw, str) and intent_raw.strip() else "(pending)"
    events_raw = payload.get("events", [])
    tool_results_raw = payload.get("tool_results", [])
    verification_raw = payload.get("verification", {})
    verification = verification_raw if isinstance(verification_raw, dict) else {}
    planner_context_raw = payload.get("telemetry", {})
    telemetry = planner_context_raw if isinstance(planner_context_raw, dict) else {}
    context_budget_raw = telemetry.get("context_budget", {})
    context_budget = context_budget_raw if isinstance(context_budget_raw, dict) else {}
    working_set_raw = context_budget.get("working_set", {})
    working_set = working_set_raw if isinstance(working_set_raw, dict) else {}
    checkpoint_raw = payload.get("checkpoint", {})
    checkpoint = checkpoint_raw if isinstance(checkpoint_raw, dict) else {}
    events = events_raw if isinstance(events_raw, list) else []
    tool_results = tool_results_raw if isinstance(tool_results_raw, list) else []
    return {
        "run_id": _trace_run_id(trace_path, payload),
        "request": request,
        "intent": intent,
        "dashboard_href": dashboard_href,
        "trace_href": trace_href,
        "events_count": len(events),
        "tool_results_count": len(tool_results),
        "verification_ok": verification.get("ok"),
        "acceptance_ok": verification.get("acceptance_ok"),
        "halt_reason": str(payload.get("halt_reason", "")).strip(),
        "working_set_tokens": working_set.get("token_estimate", 0),
        "checkpoint_thread_id": checkpoint.get("thread_id", ""),
        "checkpoint_id": checkpoint.get("latest_checkpoint_id") or checkpoint.get("resume_checkpoint_id") or "",
    }


def _trace_payload_for_run(
    trace_dir: Path,
    *,
    run_id: str,
    warn_context: str,
) -> dict[str, Any] | None:
    normalized_run_id = _validated_run_id(run_id)
    if normalized_run_id is None:
        return None
    trace_path = trace_dir / f"run-{normalized_run_id}.json"
    if not trace_path.is_file():
        return None
    return _trace_payload_from_path(trace_path, warn_context=warn_context)


def _trace_http_response(
    trace_dir: Path,
    *,
    request_path: str,
    mermaid_graph: str,
) -> tuple[int, str, bytes]:
    route = urlsplit(request_path).path.rstrip("/") or "/"
    runs: list[dict[str, Any]] = []
    for trace_path in sorted(trace_dir.glob("run-*.json"), reverse=True):
        payload = _trace_payload_from_path(trace_path, warn_context="trace_server")
        if payload is None:
            continue
        run_id = _trace_run_id(trace_path, payload)
        runs.append(
            _trace_run_summary(
                trace_path=trace_path,
                payload=payload,
                dashboard_href=f"/runs/{run_id}",
                trace_href=f"/v1/runs/{run_id}",
            )
        )

    if route in {"/", "/index.html"}:
        body = render_trace_site_index_html(runs)
        return 200, "text/html; charset=utf-8", body.encode("utf-8")

    if route == "/v1/runs":
        body = json.dumps({"runs": runs}, ensure_ascii=False, indent=2)
        return 200, "application/json; charset=utf-8", body.encode("utf-8")

    if route.startswith("/v1/runs/"):
        run_id = route.removeprefix("/v1/runs/")
        payload = _trace_payload_for_run(trace_dir, run_id=run_id, warn_context="trace_server")
        if payload is None:
            body = json.dumps({"error": "not_found", "run_id": run_id}, ensure_ascii=False)
            return 404, "application/json; charset=utf-8", body.encode("utf-8")
        body = json.dumps(payload, ensure_ascii=False, indent=2)
        return 200, "application/json; charset=utf-8", body.encode("utf-8")

    if route.startswith("/runs/"):
        run_id = route.removeprefix("/runs/")
        payload = _trace_payload_for_run(trace_dir, run_id=run_id, warn_context="trace_server")
        if payload is None:
            return 404, "text/plain; charset=utf-8", b"run not found\n"
        body = render_trace_dashboard_html(
            payload,
            mermaid_graph=mermaid_graph,
            index_href="/",
            trace_json_href=f"/v1/runs/{run_id}",
        )
        return 200, "text/html; charset=utf-8", body.encode("utf-8")

    return 404, "text/plain; charset=utf-8", b"not found\n"


def _serve_trace_http(trace_dir: Path, *, host: str, port: int) -> int:
    log = get_logger()
    mermaid_graph = export_mermaid()

    class TraceRequestHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            status, content_type, body = _trace_http_response(
                trace_dir,
                request_path=self.path,
                mermaid_graph=mermaid_graph,
            )
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    try:
        with ThreadingHTTPServer((host, port), TraceRequestHandler) as server:
            log.info("trace_server_listening", host=host, port=port, trace_dir=str(trace_dir))
            print(f"Trace server listening on http://{host}:{port}")
            server.serve_forever()
    except OSError as exc:
        log.error("trace_server_bind_failed", host=host, port=port, error=str(exc))
        return 2
    except KeyboardInterrupt:
        return 0
    return 0


def cli(argv: list[str] | None = None) -> int:
    configure_logging()
    log = get_logger()
    args = _build_parser().parse_args(argv)

    repo_root = _resolve_repo_root(repo_root_arg=getattr(args, "repo_root", None))

    if args.cmd == "export-graph":
        sys.stdout.write(export_mermaid())
        return 0

    if args.cmd == "trace-view":
        trace_path = Path(str(args.trace_path))
        try:
            payload_raw = json.loads(trace_path.read_text(encoding="utf-8"))
        except OSError as exc:
            log.error("trace_read_failed", path=str(trace_path), error=str(exc))
            return 2
        except json.JSONDecodeError as exc:
            log.error("trace_parse_failed", path=str(trace_path), error=str(exc))
            return 2

        if not isinstance(payload_raw, dict):
            log.error("trace_payload_invalid", path=str(trace_path), expected="object")
            return 2

        if str(getattr(args, "format", "console")) == "html":
            rendered = render_trace_dashboard_html(payload_raw, mermaid_graph=export_mermaid())
            output_path_raw = getattr(args, "output", None)
            if output_path_raw:
                output_path = Path(str(output_path_raw))
                try:
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    output_path.write_text(rendered, encoding="utf-8")
                except OSError as exc:
                    log.error("trace_html_write_failed", path=str(output_path), error=str(exc))
                    return 2
            else:
                sys.stdout.write(rendered)
            return 0

        width = int(args.width)
        sys.stdout.write(render_trace_dashboard(payload_raw, width=width))
        return 0

    if args.cmd == "trace-site":
        trace_dir = Path(str(args.trace_dir))
        if not trace_dir.is_dir():
            log.error("trace_site_dir_missing", path=str(trace_dir))
            return 2

        output_dir_raw = getattr(args, "output_dir", None)
        output_dir = Path(str(output_dir_raw)) if output_dir_raw else trace_dir / "site"
        trace_copy_dir = output_dir / "traces"
        mermaid_graph = export_mermaid()
        run_summaries: list[dict[str, Any]] = []

        try:
            output_dir.mkdir(parents=True, exist_ok=True)
            trace_copy_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.error("trace_site_dir_create_failed", path=str(output_dir), error=str(exc))
            return 2

        for trace_path in sorted(trace_dir.glob("run-*.json"), reverse=True):
            payload_raw = _trace_payload_from_path(trace_path, warn_context="trace_site")
            if payload_raw is None:
                continue

            dashboard_name = f"{trace_path.stem}.html"
            trace_href = f"traces/{trace_path.name}"

            try:
                dashboard_html = render_trace_dashboard_html(
                    payload_raw,
                    mermaid_graph=mermaid_graph,
                    index_href="index.html",
                    trace_json_href=trace_href,
                )
                (output_dir / dashboard_name).write_text(dashboard_html, encoding="utf-8")
                (trace_copy_dir / trace_path.name).write_text(
                    json.dumps(payload_raw, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except OSError as exc:
                log.error("trace_site_write_failed", path=str(trace_path), error=str(exc))
                return 2

            run_summaries.append(
                _trace_run_summary(
                    trace_path=trace_path,
                    payload=payload_raw,
                    dashboard_href=dashboard_name,
                    trace_href=trace_href,
                )
            )

        try:
            (output_dir / "index.html").write_text(
                render_trace_site_index_html(run_summaries),
                encoding="utf-8",
            )
        except OSError as exc:
            log.error("trace_site_index_write_failed", path=str(output_dir / 'index.html'), error=str(exc))
            return 2
        return 0

    if args.cmd == "trace-serve":
        trace_dir = Path(str(args.trace_dir))
        if not trace_dir.is_dir():
            log.error("trace_server_dir_missing", path=str(trace_dir))
            return 2

        port = int(args.port)
        if port <= 0 or port > 65535:
            log.error("trace_server_port_invalid", port=port)
            return 2
        return _serve_trace_http(trace_dir, host=str(args.host), port=port)

    if args.cmd == "serve-api":
        port = int(args.port)
        if port <= 0 or port > 65535:
            log.error("remote_api_port_invalid", port=port)
            return 2
        from lg_orch.remote_api import serve_remote_api

        return serve_remote_api(repo_root=repo_root, host=str(args.host), port=port)

    if args.cmd == "run-multi":
        from lg_orch.meta_graph import build_meta_graph

        app = build_meta_graph()
        state = {
            "request": str(args.request),
            "repositories": [str(Path(r).expanduser().resolve()) for r in args.repos],
        }

        print("\n--- Starting Lula Platform Meta-Agent ---")
        out: dict[str, Any] = {}
        for event in app.stream(state, stream_mode="updates"):
            for node_name, node_state in event.items():
                print(f"\n[Node: {node_name}]")
                if node_name == "meta_planner":
                    plan = node_state.get("meta_plan", {})
                    tasks = getattr(plan, "sub_tasks", []) if hasattr(plan, "sub_tasks") else plan.get("sub_tasks", [])
                    print(f"Generated Meta-Plan with {len(tasks)} sub-tasks.")
                elif node_name == "task_dispatcher":
                    active = node_state.get("active_tasks", [])
                    print(f"Dispatched tasks: {active}")
                elif node_name == "sub_agent_executor":
                    results = node_state.get("task_results", {})
                    completed = node_state.get("completed_tasks", [])
                    failed = node_state.get("failed_tasks", [])
                    print(f"Execution step complete. Completed: {len(completed)}, Failed: {len(failed)}")
                elif node_name == "meta_evaluator":
                    print(f"Final Report: {node_state.get('final_report')}")
                out.update(node_state)

        print("\n--- Final Output ---")
        print(out.get("final_report", ""))
        return 0

    provided_run_id = _validated_run_id(getattr(args, "run_id", None))
    if getattr(args, "run_id", None) and provided_run_id is None:
        log.error("run_id_invalid", run_id=str(args.run_id))
        return 2

    if getattr(args, "profile", None):
        import os

        os.environ["LG_PROFILE"] = str(args.profile)

    try:
        cfg = load_config(repo_root=repo_root)
    except Exception as exc:
        log.error("config_load_failed", error=str(exc), repo_root=str(repo_root))
        return 2

    runner_base_url = args.runner_base_url or cfg.runner.base_url
    trace_enabled = bool(args.trace) or cfg.trace.enabled
    trace_out_dir_raw = getattr(args, "trace_out_dir", None)
    trace_out_dir = str(trace_out_dir_raw).strip() if trace_out_dir_raw is not None else ""
    import os

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
        checkpointer = SqliteCheckpointSaver(db_path=db_path)
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
    state = {
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
            }
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

    out: dict[str, Any] = {}
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
                # Stream specific useful information
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
                status = "failed" if out.get("recovery_packet", {}).get("failure_class") else "succeeded"
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


def main(argv: list[str]) -> int:
    return cli(argv[1:])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
