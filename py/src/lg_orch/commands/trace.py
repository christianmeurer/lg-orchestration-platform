# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Christian Meurer — https://github.com/christianmeurer/Lula
"""trace_command — trace site generation and trace HTTP server.

Extracted from ``lg_orch.main.cli`` so the dispatcher stays under 200 lines.
"""
from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from lg_orch.graph import export_mermaid
from lg_orch.logging import get_logger
from lg_orch.visualize import (
    render_trace_dashboard,
    render_trace_dashboard_html,
    render_trace_site_index_html,
)


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
    from lg_orch.main import _trace_run_summary as _orig  # thin re-export

    return _orig(
        trace_path=trace_path,
        payload=payload,
        dashboard_href=dashboard_href,
        trace_href=trace_href,
    )


def trace_view_command(args: Any) -> int:
    """Render a single trace JSON file as console dashboard or HTML.

    Parameters
    ----------
    args:
        Parsed argparse namespace from the ``trace-view`` subcommand.
    """
    log = get_logger()
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


def trace_site_command(args: Any) -> int:
    """Generate a static HTML site from all trace JSON files in a directory.

    Parameters
    ----------
    args:
        Parsed argparse namespace from the ``trace-site`` subcommand.
    """
    log = get_logger()
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

        run_id = _trace_run_id(trace_path, payload_raw)
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
        log.error(
            "trace_site_index_write_failed",
            path=str(output_dir / "index.html"),
            error=str(exc),
        )
        return 2
    return 0


def trace_serve_command(args: Any) -> int:
    """Serve trace files over HTTP for browser-based inspection.

    Parameters
    ----------
    args:
        Parsed argparse namespace from the ``trace-serve`` subcommand.
    """
    log = get_logger()
    trace_dir = Path(str(args.trace_dir))
    if not trace_dir.is_dir():
        log.error("trace_server_dir_missing", path=str(trace_dir))
        return 2

    port = int(args.port)
    if port <= 0 or port > 65535:
        log.error("trace_server_port_invalid", port=port)
        return 2

    # Delegate to the private helper that lives in main (keeps it DRY)
    from lg_orch.main import _serve_trace_http

    return _serve_trace_http(trace_dir, host=str(args.host), port=port)
