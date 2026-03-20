# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Christian Meurer — https://github.com/christianmeurer/Lula
"""Entry point for the lg-orch CLI.

The ``cli()`` function is the registered ``project.scripts`` entry point.
Each subcommand delegates its heavy logic to ``lg_orch.commands.*`` so this
module stays as a thin dispatcher.  Helper functions that are imported
directly by tests (``_build_parser``, ``_trace_http_response``) and by the
commands submodules (``_validated_run_id``, ``_trace_run_summary``,
``_serve_trace_http``) are kept here for backward-compatibility.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from lg_orch.graph import export_mermaid
from lg_orch.logging import configure_logging, get_logger, init_telemetry
from lg_orch.visualize import (
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


# ---------------------------------------------------------------------------
# Trace helper functions — kept here because tests import them directly
# ---------------------------------------------------------------------------


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
    intent = (
        str(intent_raw).strip()
        if isinstance(intent_raw, str) and intent_raw.strip()
        else "(pending)"
    )
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
        "checkpoint_id": (
            checkpoint.get("latest_checkpoint_id")
            or checkpoint.get("resume_checkpoint_id")
            or ""
        ),
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


# ---------------------------------------------------------------------------
# CLI entry point — thin dispatcher
# ---------------------------------------------------------------------------


def cli(argv: list[str] | None = None) -> int:  # noqa: C901
    import os as _os

    init_telemetry(
        service_name="lula-orchestrator",
        otlp_endpoint=_os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"),
    )
    configure_logging()
    log = get_logger()
    args = _build_parser().parse_args(argv)

    repo_root = _resolve_repo_root(repo_root_arg=getattr(args, "repo_root", None))

    if args.cmd == "export-graph":
        sys.stdout.write(export_mermaid())
        return 0

    if args.cmd == "trace-view":
        from lg_orch.commands.trace import trace_view_command
        return trace_view_command(args)

    if args.cmd == "trace-site":
        from lg_orch.commands.trace import trace_site_command
        return trace_site_command(args)

    if args.cmd == "trace-serve":
        from lg_orch.commands.trace import trace_serve_command
        return trace_serve_command(args)

    if args.cmd == "serve-api":
        from lg_orch.commands.serve import serve_command
        return serve_command(args, repo_root=repo_root)

    if args.cmd == "run-multi":
        from lg_orch.meta_graph import build_meta_graph

        app = build_meta_graph()
        state: dict[str, Any] = {
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
                    tasks = (
                        getattr(plan, "sub_tasks", [])
                        if hasattr(plan, "sub_tasks")
                        else plan.get("sub_tasks", [])
                    )
                    print(f"Generated Meta-Plan with {len(tasks)} sub-tasks.")
                elif node_name == "task_dispatcher":
                    active = node_state.get("active_tasks", [])
                    print(f"Dispatched tasks: {active}")
                elif node_name == "sub_agent_executor":
                    completed = node_state.get("completed_tasks", [])
                    failed = node_state.get("failed_tasks", [])
                    print(
                        f"Execution step complete. Completed: {len(completed)}, Failed: {len(failed)}"
                    )
                elif node_name == "meta_evaluator":
                    print(f"Final Report: {node_state.get('final_report')}")
                out.update(node_state)

        print("\n--- Final Output ---")
        print(out.get("final_report", ""))
        return 0

    # "run" command
    if getattr(args, "profile", None):
        import os
        os.environ["LG_PROFILE"] = str(args.profile)

    from lg_orch.config import load_config

    try:
        cfg = load_config(repo_root=repo_root)
    except Exception as exc:
        log.error("config_load_failed", error=str(exc), repo_root=str(repo_root))
        return 2

    from lg_orch.commands.run import run_command

    return run_command(args, cfg=cfg, repo_root=repo_root)


def main(argv: list[str]) -> int:
    return cli(argv[1:])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
