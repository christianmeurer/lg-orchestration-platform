# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Christian Meurer — https://github.com/christianmeurer/Lula
"""Admin API routes — healing loop control, procedure cache, diagnostics.

These routes were previously inline in :mod:`lg_orch.remote_api` inside the
``_api_http_dispatch`` function.  Extracting them here keeps
:mod:`lg_orch.api.service` focused on run lifecycle only (start/stop/status/
list) and lets the admin surface evolve independently.

Public entry point
------------------
:func:`register_admin_routes` is called by :func:`lg_orch.remote_api._api_http_dispatch`
to handle any ``/healing/*`` or ``/admin/*`` path prefixes.
"""
from __future__ import annotations

import json
from typing import Any

from lg_orch.api.service import RemoteAPIService


def _json_response(status: int, payload: dict[str, Any]) -> tuple[int, str, bytes]:
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    return status, "application/json; charset=utf-8", body


def register_admin_routes(
    service: RemoteAPIService,
    *,
    method: str,
    route: str,
    path_parts: list[str],
    request_body: bytes | None,
) -> tuple[int, str, bytes] | None:
    """Attempt to match an admin route and return its response.

    Returns ``None`` if the route is not an admin route so the caller can
    continue dispatching to other handlers.

    Handled routes
    --------------
    * ``POST /healing/start`` — start a healing loop daemon
    * ``POST /healing/{loop_id}/stop`` — stop a healing loop daemon
    * ``GET  /healing/{loop_id}/jobs`` — list jobs for a healing loop

    Parameters
    ----------
    service:
        The :class:`~lg_orch.api.service.RemoteAPIService` instance.
    method:
        HTTP method string (``"GET"``, ``"POST"``, …).
    route:
        Normalised path string (no trailing slash).
    path_parts:
        ``route`` split on ``"/"`` with empty strings removed.
    request_body:
        Raw request body bytes, or ``None`` if empty.
    """
    # POST /healing/start
    if route == "/healing/start":
        if method != "POST":
            return _json_response(405, {"error": "method_not_allowed"})
        try:
            payload_raw = json.loads((request_body or b"{}").decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return _json_response(400, {"error": "invalid_json"})
        if not isinstance(payload_raw, dict):
            return _json_response(400, {"error": "invalid_json"})
        repo_path_raw = payload_raw.get("repo_path")
        if not isinstance(repo_path_raw, str) or not repo_path_raw.strip():
            return _json_response(400, {"error": "missing_repo_path"})
        try:
            poll_interval = float(payload_raw.get("poll_interval_seconds", 60.0))
        except (TypeError, ValueError):
            poll_interval = 60.0
        result = service.start_healing_loop(repo_path_raw.strip(), poll_interval_seconds=poll_interval)
        return _json_response(201, result)

    # POST /healing/{loop_id}/stop
    if method == "POST" and len(path_parts) == 3 and path_parts[0] == "healing" and path_parts[2] == "stop":
        loop_id = path_parts[1]
        result_stop = service.stop_healing_loop(loop_id)
        return (
            _json_response(200, result_stop)
            if result_stop is not None
            else _json_response(404, {"error": "not_found", "loop_id": loop_id})
        )

    # GET /healing/{loop_id}/jobs
    if method == "GET" and len(path_parts) == 3 and path_parts[0] == "healing" and path_parts[2] == "jobs":
        loop_id = path_parts[1]
        jobs_payload = service.get_healing_jobs(loop_id)
        return (
            _json_response(200, jobs_payload)
            if jobs_payload is not None
            else _json_response(404, {"error": "not_found", "loop_id": loop_id})
        )

    return None
