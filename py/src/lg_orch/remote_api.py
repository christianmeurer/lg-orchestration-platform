# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Christian Meurer — https://github.com/christianmeurer/Lula
"""Thin orchestrating router — assembles the Remote API from focused submodules.

Public API backward-compat aliases
-----------------------------------
* ``create_app`` / ``build_app`` — alias for ``serve_remote_api`` (factory intent).
* ``_approval_token_for_challenge`` — re-exported for test backward-compat.
* ``push_run_event`` — re-exported for test backward-compat.
* ``_stream_new_sse`` — re-exported for test backward-compat.
* ``_run_streams`` / ``_run_streams_lock`` — re-exported for test backward-compat.
* ``_RateLimiter`` / ``RunRecord`` / ``RemoteAPIService`` — re-exported.

All heavy logic lives in ``lg_orch.api.*``.  This module:
* Keeps ``_spawn_run_subprocess`` and ``_start_daemon_thread`` so that
  ``pytest.monkeypatch`` on ``remote_api._spawn_run_subprocess`` still works.
* Re-exports the public API that tests and callers depend on.
* Contains the HTTP dispatch function and ``serve_remote_api`` entry-point.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, urlsplit

# ---------------------------------------------------------------------------
# Public re-exports from submodules (backward-compatible names)
# ---------------------------------------------------------------------------
from lg_orch.api.admin import register_admin_routes  # noqa: F401
from lg_orch.api.approvals import approval_token_for_challenge as _approval_token_for_challenge  # noqa: F401
from lg_orch.api.metrics import LULA_RUNS_TOTAL, handle_metrics as _handle_metrics  # noqa: F401
from lg_orch.api.service import (  # noqa: F401
    RunRecord,
    RemoteAPIService,
    _RateLimiter,
    _apply_trace_state_to_payload,
    _non_empty_str,
    _normalized_run_id,
    _utc_now,
)
from lg_orch.api.streaming import (  # noqa: F401
    _run_streams,
    _run_streams_lock,
    push_run_event,
    stream_new_sse as _stream_new_sse,
)
from lg_orch.approval_policy import (
    QuorumApprovalPolicy,
    RoleApprovalPolicy,
    TimedApprovalPolicy,
)
from lg_orch.audit import AuditEvent, AuditLogger, build_sink, utc_now_iso
from lg_orch.auth import (
    AuthError,
    JWTSettings,
    _route_policy,
    authorize_stdlib,
    jwt_settings_from_config,
)

_JSON_CONTENT_TYPE = "application/json; charset=utf-8"
_REQUEST_ID_HEADER = "X-Request-ID"


# ---------------------------------------------------------------------------
# Primitives kept here so monkeypatching on this module still works
# ---------------------------------------------------------------------------


def _spawn_run_subprocess(
    *, argv: list[str], cwd: Path, env: dict[str, str] | None = None
) -> subprocess.Popen[str]:
    return subprocess.Popen(
        argv, cwd=str(cwd), env=env,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )


def _start_daemon_thread(*, target: Callable[[], None], name: str) -> None:
    thread = threading.Thread(target=target, name=name, daemon=True)
    thread.start()


def _json_response(status: int, payload: dict[str, Any]) -> tuple[int, str, bytes]:
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    return status, _JSON_CONTENT_TYPE, body


def _request_id_from_value(raw: object) -> str:
    import uuid
    value = _non_empty_str(raw)
    return value or uuid.uuid4().hex


def _request_client_ip(*, client_address: tuple[str, int] | None, forwarded_for: str | None, trust_forwarded_headers: bool) -> str:
    if trust_forwarded_headers and forwarded_for:
        first = forwarded_for.split(",", 1)[0].strip()
        if first:
            return first
    if client_address is not None:
        return str(client_address[0])
    return ""


def _request_scheme(*, forwarded_proto: str | None, trust_forwarded_headers: bool) -> str:
    if trust_forwarded_headers and forwarded_proto:
        first = forwarded_proto.split(",", 1)[0].strip().lower()
        if first:
            return first
    return "http"


def _authorize_request(*, route: str, auth_mode: str, expected_bearer_token: str | None, authorization_header: str | None, allow_unauthenticated_healthz: bool) -> tuple[str, tuple[int, str, bytes] | None]:
    import hmac
    if route == "/healthz" and allow_unauthenticated_healthz:
        return "", None
    if route == "/metrics":
        return "", None
    if auth_mode == "off":
        return "", None
    if auth_mode != "bearer":
        return "", _json_response(500, {"error": "unsupported_auth_mode"})
    if expected_bearer_token is None:
        return "", _json_response(503, {"error": "remote_api_auth_not_configured"})
    auth = _non_empty_str(authorization_header)
    if auth is None or auth[:7].lower() != "bearer ":
        return "", _json_response(401, {"error": "missing_bearer_token"})
    given = auth[7:].strip()
    if not hmac.compare_digest(given, expected_bearer_token):
        return "", _json_response(403, {"error": "invalid_bearer_token"})
    return "bearer", None


def _audit_action_and_resource(*, method: str, route: str, path_parts: list[str], status: int) -> tuple[str, str | None]:
    if method == "POST" and route in {"/v1/runs", "/runs", "/runs/"}:
        return "run.create", None
    if method == "GET" and route in {"/v1/runs", "/runs", "/runs/"}:
        return "run.list", None
    if route == "/runs/search" and method == "GET":
        return "run.search", None
    if method == "GET" and len(path_parts) == 3 and path_parts[:2] == ["v1", "runs"]:
        return "run.read", path_parts[2]
    if method == "GET" and len(path_parts) == 2 and path_parts[0] == "runs":
        return "run.read", path_parts[1]
    if len(path_parts) == 4 and path_parts[:2] == ["v1", "runs"] and path_parts[3] == "cancel":
        return "run.cancel", path_parts[2]
    if method == "POST" and len(path_parts) == 3 and path_parts[0] == "runs" and path_parts[2] == "cancel":
        return "run.cancel", path_parts[1]
    if len(path_parts) == 4 and path_parts[:2] == ["v1", "runs"] and path_parts[3] in {"approve", "reject"}:
        return "run.approve", path_parts[2]
    if method == "POST" and len(path_parts) == 3 and path_parts[0] == "runs" and path_parts[2] in {"approve", "reject"}:
        return "run.approve", path_parts[1]
    if len(path_parts) >= 3 and path_parts[-1] in {"logs", "stream"}:
        rid = path_parts[-2] if len(path_parts) >= 3 else None
        return "run.read", rid
    return "api.request", None


# ---------------------------------------------------------------------------
# Module-level audit logger (set by serve_remote_api)
# ---------------------------------------------------------------------------
_audit_logger: AuditLogger | None = None


# ---------------------------------------------------------------------------
# Route handler functions (extracted from the former if/elif chain)
# ---------------------------------------------------------------------------


def _hdl_metrics(
    service: "RemoteAPIService", method: str, request_path: str,
    request_body: bytes | None, auth_subject: str, path_parts: list[str],
    request_id: str, client_ip: str,
) -> tuple[int, str, bytes]:
    return _handle_metrics(method)


def _hdl_root_ui(
    service: "RemoteAPIService", method: str, request_path: str,
    request_body: bytes | None, auth_subject: str, path_parts: list[str],
    request_id: str, client_ip: str,
) -> tuple[int, str, bytes]:
    if method != "GET":
        return _json_response(405, {"error": "method_not_allowed"})
    from lg_orch.graph import export_mermaid
    from lg_orch.visualize import render_run_viewer_spa
    html = render_run_viewer_spa(api_base_url="", mermaid_graph=export_mermaid())
    return 200, "text/html; charset=utf-8", html.encode("utf-8")


def _hdl_healthz(
    service: "RemoteAPIService", method: str, request_path: str,
    request_body: bytes | None, auth_subject: str, path_parts: list[str],
    request_id: str, client_ip: str,
) -> tuple[int, str, bytes]:
    if method != "GET":
        return _json_response(405, {"error": "method_not_allowed"})
    return _json_response(200, {"ok": True})


def _hdl_v1_runs(
    service: "RemoteAPIService", method: str, request_path: str,
    request_body: bytes | None, auth_subject: str, path_parts: list[str],
    request_id: str, client_ip: str,
) -> tuple[int, str, bytes]:
    if method == "GET":
        return _json_response(200, {"runs": service.list_runs()})
    if method != "POST":
        return _json_response(405, {"error": "method_not_allowed"})
    try:
        payload_raw = json.loads((request_body or b"{}").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _json_response(400, {"error": "invalid_json"})
    if not isinstance(payload_raw, dict):
        return _json_response(400, {"error": "invalid_json"})
    try:
        return _json_response(201, service.create_run(payload_raw, request_id=request_id, auth_subject=auth_subject, client_ip=client_ip))
    except ValueError as exc:
        error = str(exc)
        return _json_response(409 if error == "duplicate_run_id" else 400, {"error": error})
    except OSError as exc:
        return _json_response(500, {"error": "launch_failed", "detail": str(exc)})


def _hdl_runs_list(
    service: "RemoteAPIService", method: str, request_path: str,
    request_body: bytes | None, auth_subject: str, path_parts: list[str],
    request_id: str, client_ip: str,
) -> tuple[int, str, bytes]:
    if method != "GET":
        return _json_response(405, {"error": "method_not_allowed"})
    return _json_response(200, {"runs": service.list_runs()})


def _hdl_runs_search(
    service: "RemoteAPIService", method: str, request_path: str,
    request_body: bytes | None, auth_subject: str, path_parts: list[str],
    request_id: str, client_ip: str,
) -> tuple[int, str, bytes]:
    if method != "GET":
        return _json_response(405, {"error": "method_not_allowed"})
    qs = parse_qs(urlsplit(request_path).query, keep_blank_values=False)
    q_values = qs.get("q", [])
    if not q_values or not q_values[0].strip():
        return _json_response(422, {"error": "missing_required_param", "param": "q"})
    q = q_values[0].strip()
    limit_raw = qs.get("limit", ["50"])[0]
    try:
        limit = max(1, min(200, int(limit_raw)))
    except ValueError:
        limit = 50
    results = service.search_runs(q, limit=limit)
    return _json_response(200, {"results": results, "total": len(results)})


# ---------------------------------------------------------------------------
# Parameterized route handlers (path_parts-based dispatch)
# ---------------------------------------------------------------------------


def _hdl_v1_run_logs(
    service: "RemoteAPIService", method: str, request_path: str,
    request_body: bytes | None, auth_subject: str, path_parts: list[str],
    request_id: str, client_ip: str,
) -> tuple[int, str, bytes]:
    if method != "GET":
        return _json_response(405, {"error": "method_not_allowed"})
    run_id = path_parts[2]
    payload = service.get_logs(run_id)
    return _json_response(200, payload) if payload is not None else _json_response(404, {"error": "not_found", "run_id": run_id})


def _hdl_v1_run_cancel(
    service: "RemoteAPIService", method: str, request_path: str,
    request_body: bytes | None, auth_subject: str, path_parts: list[str],
    request_id: str, client_ip: str,
) -> tuple[int, str, bytes]:
    if method != "POST":
        return _json_response(405, {"error": "method_not_allowed"})
    run_id = path_parts[2]
    payload = service.cancel_run(run_id)
    return _json_response(202, payload) if payload is not None else _json_response(404, {"error": "not_found", "run_id": run_id})


def _hdl_v1_run_approve_reject(
    service: "RemoteAPIService", method: str, request_path: str,
    request_body: bytes | None, auth_subject: str, path_parts: list[str],
    request_id: str, client_ip: str,
) -> tuple[int, str, bytes]:
    if method != "POST":
        return _json_response(405, {"error": "method_not_allowed"})
    try:
        payload_raw = json.loads((request_body or b"{}").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _json_response(400, {"error": "invalid_json"})
    if not isinstance(payload_raw, dict):
        return _json_response(400, {"error": "invalid_json"})
    run_id = path_parts[2]
    try:
        payload = (
            service.approve_run(run_id, payload_raw, auth_subject=auth_subject)
            if path_parts[3] == "approve"
            else service.reject_run(run_id, payload_raw, auth_subject=auth_subject)
        )
    except ValueError as exc:
        return _json_response(409, {"error": str(exc), "run_id": run_id})
    return _json_response(202, payload) if payload is not None else _json_response(404, {"error": "not_found", "run_id": run_id})


def _hdl_v1_run_stream(
    service: "RemoteAPIService", method: str, request_path: str,
    request_body: bytes | None, auth_subject: str, path_parts: list[str],
    request_id: str, client_ip: str,
) -> tuple[int, str, bytes]:
    return -1, "sse", path_parts[2].encode("utf-8")  # type: ignore[return-value]


def _hdl_v1_run_get(
    service: "RemoteAPIService", method: str, request_path: str,
    request_body: bytes | None, auth_subject: str, path_parts: list[str],
    request_id: str, client_ip: str,
) -> tuple[int, str, bytes]:
    if method != "GET":
        return _json_response(405, {"error": "method_not_allowed"})
    run_id = path_parts[2]
    payload = service.get_run(run_id)
    return _json_response(200, payload) if payload is not None else _json_response(404, {"error": "not_found", "run_id": run_id})


def _hdl_runs_stream(
    service: "RemoteAPIService", method: str, request_path: str,
    request_body: bytes | None, auth_subject: str, path_parts: list[str],
    request_id: str, client_ip: str,
) -> tuple[int, str, bytes]:
    _sse_run_id = path_parts[1]
    if service.get_run(_sse_run_id) is None:
        return _json_response(404, {"error": "not_found", "run_id": _sse_run_id})
    return -2, "sse_new", _sse_run_id.encode("utf-8")  # type: ignore[return-value]


def _hdl_runs_approve_reject(
    service: "RemoteAPIService", method: str, request_path: str,
    request_body: bytes | None, auth_subject: str, path_parts: list[str],
    request_id: str, client_ip: str,
) -> tuple[int, str, bytes]:
    try:
        payload_raw = json.loads((request_body or b"{}").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _json_response(400, {"error": "invalid_json"})
    if not isinstance(payload_raw, dict):
        return _json_response(400, {"error": "invalid_json"})
    run_id = path_parts[1]
    try:
        payload = (
            service.approve_run(run_id, payload_raw, auth_subject=auth_subject)
            if path_parts[2] == "approve"
            else service.reject_run(run_id, payload_raw, auth_subject=auth_subject)
        )
    except ValueError as exc:
        return _json_response(409, {"error": str(exc), "run_id": run_id})
    return _json_response(202, payload) if payload is not None else _json_response(404, {"error": "not_found", "run_id": run_id})


def _hdl_runs_approval_policy(
    service: "RemoteAPIService", method: str, request_path: str,
    request_body: bytes | None, auth_subject: str, path_parts: list[str],
    request_id: str, client_ip: str,
) -> tuple[int, str, bytes]:
    try:
        payload_raw = json.loads((request_body or b"{}").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _json_response(400, {"error": "invalid_json"})
    if not isinstance(payload_raw, dict):
        return _json_response(400, {"error": "invalid_json"})
    policy_raw = payload_raw.get("policy")
    if not isinstance(policy_raw, dict):
        return _json_response(400, {"error": "missing_policy"})
    kind = policy_raw.get("kind")
    if kind == "timed":
        from typing import cast as _cast
        policy = TimedApprovalPolicy(
            timeout_seconds=float(policy_raw.get("timeout_seconds", 300.0)),
            auto_action=_cast("Literal['approve', 'reject']", policy_raw.get("auto_action", "reject")),
        )
    elif kind == "quorum":
        policy = QuorumApprovalPolicy(
            required_approvals=int(policy_raw.get("required_approvals", 1)),
            required_rejections=int(policy_raw.get("required_rejections", 1)),
            allowed_reviewers=list(policy_raw.get("allowed_reviewers", [])),
        )
    elif kind == "role":
        policy = RoleApprovalPolicy(
            required_roles=list(policy_raw.get("required_roles", [])),
            require_all_roles=bool(policy_raw.get("require_all_roles", False)),
        )
    else:
        return _json_response(400, {"error": "unknown_policy_kind"})
    run_id = path_parts[1]
    return _json_response(200, service.set_approval_policy(run_id, policy))


def _hdl_runs_vote(
    service: "RemoteAPIService", method: str, request_path: str,
    request_body: bytes | None, auth_subject: str, path_parts: list[str],
    request_id: str, client_ip: str,
) -> tuple[int, str, bytes]:
    try:
        payload_raw = json.loads((request_body or b"{}").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _json_response(400, {"error": "invalid_json"})
    if not isinstance(payload_raw, dict):
        return _json_response(400, {"error": "invalid_json"})
    run_id = path_parts[1]
    reviewer_id = _non_empty_str(payload_raw.get("reviewer_id"))
    if reviewer_id is None:
        return _json_response(400, {"error": "missing_reviewer_id"})
    action = _non_empty_str(payload_raw.get("action"))
    if action not in {"approve", "reject"}:
        return _json_response(400, {"error": "invalid_action"})
    role_raw = payload_raw.get("role")
    role = _non_empty_str(role_raw) if role_raw is not None else None
    comment = str(payload_raw.get("comment", ""))
    try:
        result = service.cast_vote(run_id, reviewer_id=reviewer_id, role=role, action=action, comment=comment)
    except KeyError:
        return _json_response(404, {"error": "policy_not_found", "run_id": run_id})
    return _json_response(200, result)


def _hdl_spa(
    service: "RemoteAPIService", method: str, request_path: str,
    request_body: bytes | None, auth_subject: str, path_parts: list[str],
    request_id: str, client_ip: str,
) -> tuple[int, str, bytes]:
    if method != "GET":
        return _json_response(405, {"error": "method_not_allowed"})
    spa_dir = Path(__file__).parent / "spa"
    if not spa_dir.exists():
        return _json_response(503, {"error": "spa_not_available"})
    from lg_orch.spa.router import create_spa_router
    subpath = "/".join(path_parts[1:]) if len(path_parts) > 1 else ""
    return create_spa_router(spa_dir)(subpath)


# ---------------------------------------------------------------------------
# Dispatch table — exact routes
# ---------------------------------------------------------------------------

_HandlerFn = Callable[
    ["RemoteAPIService", str, str, "bytes | None", str, "list[str]", str, str],
    "tuple[int, str, bytes]",
]

_EXACT_ROUTE_TABLE: dict[str, _HandlerFn] = {
    "/metrics": _hdl_metrics,
    "/": _hdl_root_ui,
    "/ui": _hdl_root_ui,
    "/healthz": _hdl_healthz,
    "/v1/runs": _hdl_v1_runs,
    "/runs": _hdl_runs_list,
    "/runs/": _hdl_runs_list,
    "/runs/search": _hdl_runs_search,
}


def _match_parameterized(
    route: str, method: str, path_parts: list[str],
) -> _HandlerFn | None:
    """Return the handler for a parameterized route, or None if no match."""
    n = len(path_parts)
    # /v1/runs/{run_id}/logs
    if n == 4 and path_parts[:2] == ["v1", "runs"] and path_parts[3] == "logs":
        return _hdl_v1_run_logs
    # /v1/runs/{run_id}/cancel
    if n == 4 and path_parts[:2] == ["v1", "runs"] and path_parts[3] == "cancel":
        return _hdl_v1_run_cancel
    # /v1/runs/{run_id}/approve|reject
    if n == 4 and path_parts[:2] == ["v1", "runs"] and path_parts[3] in {"approve", "reject"}:
        return _hdl_v1_run_approve_reject
    # /v1/runs/{run_id}/stream
    if n == 4 and path_parts[:2] == ["v1", "runs"] and path_parts[3] == "stream":
        return _hdl_v1_run_stream
    # /v1/runs/{run_id}
    if n == 3 and path_parts[:2] == ["v1", "runs"]:
        return _hdl_v1_run_get
    # /runs/{run_id}/stream
    if method == "GET" and n == 3 and path_parts[0] == "runs" and path_parts[2] == "stream":
        return _hdl_runs_stream
    # /runs/{run_id}/approve|reject
    if method == "POST" and n == 3 and path_parts[0] == "runs" and path_parts[2] in {"approve", "reject"}:
        return _hdl_runs_approve_reject
    # /runs/{run_id}/approval-policy
    if method == "POST" and n == 3 and path_parts[0] == "runs" and path_parts[2] == "approval-policy":
        return _hdl_runs_approval_policy
    # /runs/{run_id}/vote
    if method == "POST" and n == 3 and path_parts[0] == "runs" and path_parts[2] == "vote":
        return _hdl_runs_vote
    # /app/...
    if path_parts and path_parts[0] == "app":
        return _hdl_spa
    return None


def _api_http_dispatch(
    service: RemoteAPIService,
    *,
    method: str,
    request_path: str,
    request_body: bytes | None,
    request_id: str = "",
    client_ip: str = "",
    auth_mode: str = "off",
    expected_bearer_token: str | None = None,
    authorization_header: str | None = None,
    allow_unauthenticated_healthz: bool = True,
    jwt_settings: JWTSettings | None = None,
) -> tuple[int, str, bytes]:
    route = urlsplit(request_path).path.rstrip("/") or "/"
    auth_subject, auth_error = _authorize_request(
        route=route, auth_mode=auth_mode, expected_bearer_token=expected_bearer_token,
        authorization_header=authorization_header,
        allow_unauthenticated_healthz=allow_unauthenticated_healthz,
    )
    if auth_error is not None:
        _pp = [p for p in route.split("/") if p]
        _action, _rid = _audit_action_and_resource(method=method, route=route, path_parts=_pp, status=auth_error[0])
        if _audit_logger is not None:
            _audit_logger.log(AuditEvent(ts=utc_now_iso(), subject="anonymous", roles=[], action=_action, resource_id=_rid, outcome="denied", detail="bearer_auth_failed"))
        return auth_error

    if service._rate_limiter is not None and not service._rate_limiter.acquire():
        return _json_response(429, {"error": "rate_limit_exceeded"})

    _jwt = jwt_settings or JWTSettings(jwt_secret=None, jwks_url=None)
    path_parts = [p for p in route.split("/") if p]
    _required_roles = _route_policy(route=route, method=method, path_parts=path_parts, jwt_enabled=_jwt.enabled)
    if _required_roles:
        try:
            _claims = authorize_stdlib(authorization=authorization_header, settings=_jwt, required_roles=_required_roles)
            if _claims.sub and _claims.sub != "anonymous":
                auth_subject = _claims.sub
        except AuthError as _auth_exc:
            _action, _rid = _audit_action_and_resource(method=method, route=route, path_parts=path_parts, status=_auth_exc.status_code)
            if _audit_logger is not None:
                _audit_logger.log(AuditEvent(ts=utc_now_iso(), subject="anonymous", roles=[], action=_action, resource_id=_rid, outcome="denied", detail=_auth_exc.detail))
            return _json_response(_auth_exc.status_code, {"error": _auth_exc.detail})

    handler: _HandlerFn | None = _EXACT_ROUTE_TABLE.get(route)
    if handler is None:
        handler = _match_parameterized(route, method, path_parts)
    if handler is not None:
        return handler(service, method, request_path, request_body, auth_subject, path_parts, request_id, client_ip)

    _admin_response = register_admin_routes(service, method=method, route=route, path_parts=path_parts, request_body=request_body)
    if _admin_response is not None:
        return _admin_response

    return _json_response(404, {"error": "not_found"})


def _api_http_response(
    service: RemoteAPIService,
    *,
    method: str,
    request_path: str,
    request_body: bytes | None,
    request_id: str = "",
    client_ip: str = "",
    auth_mode: str = "off",
    expected_bearer_token: str | None = None,
    authorization_header: str | None = None,
    allow_unauthenticated_healthz: bool = True,
    jwt_settings: JWTSettings | None = None,
) -> tuple[int, str, bytes]:
    """Public entry point: wraps ``_api_http_dispatch`` with audit emission."""
    status, content_type, body = _api_http_dispatch(
        service, method=method, request_path=request_path, request_body=request_body,
        request_id=request_id, client_ip=client_ip, auth_mode=auth_mode,
        expected_bearer_token=expected_bearer_token, authorization_header=authorization_header,
        allow_unauthenticated_healthz=allow_unauthenticated_healthz, jwt_settings=jwt_settings,
    )
    if _audit_logger is not None and status not in {401, 403}:
        _route = urlsplit(request_path).path.rstrip("/") or "/"
        _pp = [p for p in _route.split("/") if p]
        _action, _rid = _audit_action_and_resource(method=method, route=_route, path_parts=_pp, status=status)
        _outcome: Literal["ok", "denied", "error"] = "error" if status >= 500 else "ok"
        _audit_logger.log(AuditEvent(ts=utc_now_iso(), subject=auth_mode if auth_mode != "off" else "anonymous", roles=[], action=_action, resource_id=_rid, outcome=_outcome, detail=None))
    return status, content_type, body


def serve_remote_api(*, repo_root: Path, host: str, port: int) -> int:
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from lg_orch.config import load_config
    from lg_orch.logging import get_logger, init_telemetry
    from lg_orch.procedure_cache import ProcedureCache
    from lg_orch.run_store import RunStore

    init_telemetry(service_name="lula-orchestrator", otlp_endpoint=os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"))
    log = get_logger()
    try:
        cfg = load_config(repo_root=repo_root)
    except Exception as exc:
        log.error("remote_api_config_load_failed", error=str(exc), repo_root=str(repo_root))
        return 2

    remote_api_cfg = cfg.remote_api
    _namespace = remote_api_cfg.default_namespace
    run_store: RunStore | None = None
    if remote_api_cfg.run_store_path:
        run_store = RunStore(db_path=Path(remote_api_cfg.run_store_path), namespace=_namespace)
    rate_limiter: _RateLimiter | None = None
    if remote_api_cfg.rate_limit_rps > 0:
        rps = remote_api_cfg.rate_limit_rps
        rate_limiter = _RateLimiter(capacity=max(rps * 2, 10), rate=float(rps))
    procedure_cache: ProcedureCache | None = None
    if remote_api_cfg.procedure_cache_path:
        procedure_cache = ProcedureCache(db_path=Path(remote_api_cfg.procedure_cache_path))
    service = RemoteAPIService(repo_root=repo_root, run_store=run_store, rate_limiter=rate_limiter, procedure_cache=procedure_cache, namespace=_namespace)
    _jwt_settings = jwt_settings_from_config(jwt_secret=remote_api_cfg.jwt_secret, jwks_url=remote_api_cfg.jwks_url)

    class RemoteAPIRequestHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self._handle_request(method="GET")

        def do_POST(self) -> None:
            self._handle_request(method="POST")

        def _handle_request(self, *, method: str) -> None:
            request_id = _request_id_from_value(self.headers.get(_REQUEST_ID_HEADER))
            route = urlsplit(self.path).path.rstrip("/") or "/"
            client_ip = _request_client_ip(client_address=self.client_address, forwarded_for=_non_empty_str(self.headers.get("X-Forwarded-For")), trust_forwarded_headers=remote_api_cfg.trust_forwarded_headers)
            scheme = _request_scheme(forwarded_proto=_non_empty_str(self.headers.get("X-Forwarded-Proto")), trust_forwarded_headers=remote_api_cfg.trust_forwarded_headers)
            started_at = time.perf_counter()
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                content_length = 0
            try:
                request_body = self.rfile.read(content_length) if content_length > 0 else None
                status, content_type, body = _api_http_response(service, method=method, request_path=self.path, request_body=request_body, request_id=request_id, client_ip=client_ip, auth_mode=remote_api_cfg.auth_mode, expected_bearer_token=remote_api_cfg.bearer_token, authorization_header=self.headers.get("Authorization"), allow_unauthenticated_healthz=remote_api_cfg.allow_unauthenticated_healthz, jwt_settings=_jwt_settings)
            except Exception as exc:
                log.error("remote_api_request_failed", request_id=request_id, method=method, route=route, client_ip=client_ip, error=str(exc))
                status, content_type, body = _json_response(500, {"error": "internal_server_error"})

            if status == -1 and content_type == "sse":
                sse_run_id = body.decode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Accel-Buffering", "no")
                self.send_header(_REQUEST_ID_HEADER, request_id)
                self.end_headers()
                service.stream_run_sse(sse_run_id, self.wfile)
                return

            if status == -2 and content_type == "sse_new":
                sse_run_id = body.decode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Accel-Buffering", "no")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header(_REQUEST_ID_HEADER, request_id)
                self.end_headers()
                _stream_new_sse(service, sse_run_id, self.wfile)
                return

            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header(_REQUEST_ID_HEADER, request_id)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            if remote_api_cfg.access_log_enabled:
                duration_ms = int((time.perf_counter() - started_at) * 1000)
                log.info("remote_api_access", request_id=request_id, method=method, route=route, status=status, duration_ms=duration_ms, client_ip=client_ip, scheme=scheme, authenticated=bool(status < 400 and remote_api_cfg.auth_mode != "off"))

        def log_message(self, format: str, *args: object) -> None:
            return

    global _audit_logger
    audit_cfg = cfg.audit
    _audit_sink = build_sink(audit_cfg)
    _audit_logger = AuditLogger(log_path=Path(audit_cfg.log_path), sink=_audit_sink)

    try:
        with ThreadingHTTPServer((host, port), RemoteAPIRequestHandler) as server:
            log.info("remote_api_listening", host=host, port=port, repo_root=str(repo_root), auth_mode=remote_api_cfg.auth_mode, trust_forwarded_headers=remote_api_cfg.trust_forwarded_headers)
            print(f"Remote API listening on http://{host}:{port}")
            server.serve_forever()
    except OSError as exc:
        log.error("remote_api_bind_failed", host=host, port=port, error=str(exc))
        return 2
    except KeyboardInterrupt:
        return 0
    finally:
        if _audit_logger is not None:
            _audit_logger.close()
            _audit_logger = None
    return 0


# ---------------------------------------------------------------------------
# Factory aliases — kept for backward compatibility
# ---------------------------------------------------------------------------


def create_app(*, repo_root: Path, host: str = "127.0.0.1", port: int = 8001) -> int:
    """Alias for :func:`serve_remote_api` kept for backward compatibility."""
    return serve_remote_api(repo_root=repo_root, host=host, port=port)


build_app = create_app
