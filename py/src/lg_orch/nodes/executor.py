# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Christian Meurer — https://github.com/christianmeurer/Lula
from __future__ import annotations

import fnmatch
import json
from typing import Any

from lg_orch.logging import get_logger
from lg_orch.memory import ensure_history_policy, prune_pre_verification_history
from lg_orch.model_routing import tool_routing_metadata
from lg_orch.nodes._utils import validate_base_url as _validate_base_url_fn
from lg_orch.tools import RunnerClient
from lg_orch.trace import append_event


def _validate_base_url(url: str) -> bool:
    try:
        _validate_base_url_fn(url, "runner_base_url")
        return True
    except ValueError:
        return False


def _as_int(value: object, *, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return default
    return default


def _estimate_patch_bytes(input_payload: dict[str, Any]) -> int:
    patch = input_payload.get("patch")
    if isinstance(patch, str):
        return len(patch.encode("utf-8", errors="replace"))

    changes_raw = input_payload.get("changes", [])
    if isinstance(changes_raw, list):
        total = 0
        for change in changes_raw:
            if not isinstance(change, dict):
                continue
            for key in ("content", "patch"):
                raw = change.get(key)
                if isinstance(raw, str):
                    total += len(raw.encode("utf-8", errors="replace"))
        if total > 0:
            return total

    return len(json.dumps(input_payload, ensure_ascii=False).encode("utf-8", errors="replace"))


def _budget_failure_result(
    *,
    tool: str,
    message: str,
    error_tag: str,
    route_metadata: dict[str, Any],
    artifacts_extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    artifacts = {"error": error_tag}
    if artifacts_extra:
        artifacts.update(artifacts_extra)
    return {
        "tool": tool,
        "ok": False,
        "exit_code": 1,
        "stdout": "",
        "stderr": message,
        "diagnostics": [],
        "timing_ms": 0,
        "artifacts": artifacts,
        "route": route_metadata,
    }


def _normalize_rel_path(path: str) -> str:
    return path.strip().replace("\\", "/")


def _coerce_approval_token(raw: object) -> dict[str, str] | None:
    """Validate and extract an approval token dict.

    Accepts tokens in both the legacy plain-text format (``approve:<id>``) and
    the current HMAC format (``<challenge_id>|<iat>|<nonce>|<signature>``).
    Structural integrity is checked here; cryptographic verification is
    delegated to the Rust runner.
    """
    if not isinstance(raw, dict):
        return None
    challenge_id = raw.get("challenge_id")
    token = raw.get("token")
    if not isinstance(challenge_id, str) or not challenge_id.strip():
        return None
    if not isinstance(token, str) or not token.strip():
        return None
    token_s = token.strip()
    parts = token_s.split("|")
    if len(parts) != 1 and len(parts) != 4:
        return None
    if len(parts) == 4:
        if not all(parts):
            return None
    return {"challenge_id": challenge_id.strip(), "token": token_s}


def _approval_for_tool(
    state: dict[str, Any], *, tool_name: str, input_payload: dict[str, Any]
) -> dict[str, str] | None:
    direct = _coerce_approval_token(input_payload.get("approval"))
    if direct is not None:
        return direct

    approvals_raw = state.get("approvals", {})
    if not isinstance(approvals_raw, dict) or not approvals_raw:
        resumed_raw = state.get("_resume_approvals", {})
        approvals_raw = dict(resumed_raw) if isinstance(resumed_raw, dict) else {}
    if not approvals_raw:
        return None

    tool_approval = _coerce_approval_token(approvals_raw.get(tool_name))
    if tool_approval is not None:
        return tool_approval

    mutations_raw = approvals_raw.get("mutations")
    if not isinstance(mutations_raw, dict):
        return None
    return _coerce_approval_token(mutations_raw.get(tool_name))


def _configured_write_allowlist(guards: dict[str, Any]) -> tuple[str, ...]:
    raw = guards.get("allowed_write_paths", [])
    if not isinstance(raw, list):
        return ()
    patterns = [_normalize_rel_path(entry) for entry in raw if isinstance(entry, str) and entry.strip()]
    return tuple(patterns)


def _apply_patch_changed_paths(input_payload: dict[str, Any]) -> list[str] | None:
    changes_raw = input_payload.get("changes", [])
    if not isinstance(changes_raw, list) or not changes_raw:
        return None

    paths: list[str] = []
    for change in changes_raw:
        if not isinstance(change, dict):
            return None
        path = change.get("path")
        if not isinstance(path, str) or not path.strip():
            return None
        paths.append(_normalize_rel_path(path))
    return paths


def _path_matches_allowlist(path: str, allowed_write_paths: tuple[str, ...]) -> bool:
    normalized = _normalize_rel_path(path)
    return any(fnmatch.fnmatch(normalized, pattern) for pattern in allowed_write_paths)


def executor(state: dict[str, Any]) -> dict[str, Any]:
    log = get_logger()
    if bool(state.get("_runner_enabled", True)) is False:
        return state
    state = ensure_history_policy(state)

    state = append_event(state, kind="node", data={"name": "executor", "phase": "start"})

    plan = state.get("plan")
    if not isinstance(plan, dict):
        return state

    runner_base_url = str(state.get("_runner_base_url", "http://127.0.0.1:8088"))
    if not _validate_base_url(runner_base_url):
        log.error("executor_invalid_base_url", url=runner_base_url)
        return append_event(
            state,
            kind="node",
            data={
                "name": "executor",
                "phase": "end",
                "error": "invalid_base_url",
            },
        )

    api_key = state.get("_runner_api_key")
    api_key_s = str(api_key).strip() if api_key is not None else None
    request_id = state.get("_request_id")
    request_id_s = str(request_id).strip() if request_id is not None else None
    try:
        client = RunnerClient(base_url=runner_base_url, api_key=api_key_s, request_id=request_id_s)
    except Exception as exc:
        log.error("executor_client_init_failed", error=str(exc))
        return append_event(
            state,
            kind="node",
            data={
                "name": "executor",
                "phase": "end",
                "error": "client_init_failed",
            },
        )

    tool_results: list[dict[str, Any]] = list(state.get("tool_results", []))
    budgets_raw = state.get("budgets", {})
    budgets = dict(budgets_raw) if isinstance(budgets_raw, dict) else {}
    guards_raw = state.get("guards", {})
    guards = dict(guards_raw) if isinstance(guards_raw, dict) else {}
    max_tool_calls = _as_int(state.get("_budget_max_tool_calls_per_loop", 0), default=0)
    max_patch_bytes = _as_int(state.get("_budget_max_patch_bytes", 0), default=0)
    tool_calls_used = _as_int(budgets.get("tool_calls_used", 0), default=0)
    require_approval_for_mutations = bool(guards.get("require_approval_for_mutations", False))
    allowed_write_paths = _configured_write_allowlist(guards)
    checkpoint_state_raw = state.get("_checkpoint", {})
    checkpoint_state = (
        dict(checkpoint_state_raw) if isinstance(checkpoint_state_raw, dict) else {}
    )
    stop_execution = False
    for step in plan.get("steps", []):
        if stop_execution:
            break
        try:
            calls: list[dict[str, Any]] = []
            route_metadata = tool_routing_metadata(state, stage="executor")
            planned_tools = step.get("tools", [])
            if max_tool_calls > 0 and tool_calls_used + len(planned_tools) > max_tool_calls:
                tool_results.append(
                    _budget_failure_result(
                        tool="batch_execute",
                        message=(
                            f"tool-call budget exceeded: used={tool_calls_used} "
                            f"planned={len(planned_tools)} limit={max_tool_calls}"
                        ),
                        error_tag="tool_call_budget_exceeded",
                        route_metadata=route_metadata,
                    )
                )
                stop_execution = True
                break
            for tool_call in step.get("tools", []):
                input_payload = dict(tool_call.get("input", {}))
                tool_name = str(tool_call.get("tool"))
                if tool_name == "apply_patch":
                    if require_approval_for_mutations:
                        approval = _approval_for_tool(
                            state,
                            tool_name=tool_name,
                            input_payload=input_payload,
                        )
                        if approval is None:
                            tool_results.append(
                                _budget_failure_result(
                                    tool=tool_name,
                                    message="mutation approval required before apply_patch execution",
                                    error_tag="approval_required",
                                    route_metadata=route_metadata,
                                    artifacts_extra={
                                        "approval": {
                                            "required": True,
                                            "status": "challenge_required",
                                            "operation_class": "apply_patch",
                                            "challenge_id": "approval:apply_patch",
                                            "reason": "missing_approval_token",
                                        }
                                    },
                                )
                            )
                            calls = []
                            stop_execution = True
                            break
                        input_payload["approval"] = approval

                    if allowed_write_paths:
                        changed_paths = _apply_patch_changed_paths(input_payload)
                        if not changed_paths:
                            tool_results.append(
                                _budget_failure_result(
                                    tool=tool_name,
                                    message="apply_patch requires explicit change paths for write allowlist enforcement",
                                    error_tag="write_path_not_allowed",
                                    route_metadata=route_metadata,
                                    artifacts_extra={
                                        "allowed_write_paths": list(allowed_write_paths),
                                    },
                                )
                            )
                            calls = []
                            stop_execution = True
                            break

                        denied_path = next(
                            (
                                path
                                for path in changed_paths
                                if not _path_matches_allowlist(path, allowed_write_paths)
                            ),
                            None,
                        )
                        if denied_path is not None:
                            tool_results.append(
                                _budget_failure_result(
                                    tool=tool_name,
                                    message=(
                                        f"write path denied by policy allowlist: path={denied_path}"
                                    ),
                                    error_tag="write_path_not_allowed",
                                    route_metadata=route_metadata,
                                    artifacts_extra={
                                        "path": denied_path,
                                        "allowed_write_paths": list(allowed_write_paths),
                                    },
                                )
                            )
                            calls = []
                            stop_execution = True
                            break

                if tool_name == "apply_patch" and max_patch_bytes > 0:
                    patch_bytes = _estimate_patch_bytes(input_payload)
                    if patch_bytes > max_patch_bytes:
                        tool_results.append(
                            _budget_failure_result(
                                tool=tool_name,
                                message=(
                                    f"patch-size budget exceeded: patch_bytes={patch_bytes} "
                                    f"limit={max_patch_bytes}"
                                ),
                                error_tag="patch_size_budget_exceeded",
                                route_metadata=route_metadata,
                            )
                        )
                        calls = []
                        stop_execution = True
                        break
                input_payload["_route"] = route_metadata
                if checkpoint_state:
                    input_payload["_checkpoint"] = dict(checkpoint_state)
                calls.append(
                    {
                        "tool": tool_name,
                        "input": input_payload,
                    }
                )
            if calls:
                batch_results = client.batch_execute_tools(calls=calls)
                tool_results.extend(batch_results)
                tool_calls_used += len(calls)

                for result in batch_results:
                    snapshot_meta = result.get("snapshot")
                    if isinstance(snapshot_meta, dict):
                        checkpoint = snapshot_meta.get("checkpoint")
                        if isinstance(checkpoint, dict):
                            thread_id = checkpoint.get("thread_id")
                            if isinstance(thread_id, str) and thread_id.strip():
                                checkpoint_state["thread_id"] = thread_id.strip()
                            checkpoint_ns = checkpoint.get("checkpoint_ns")
                            if isinstance(checkpoint_ns, str):
                                checkpoint_state["checkpoint_ns"] = checkpoint_ns
                            checkpoint_id = checkpoint.get("checkpoint_id")
                            if isinstance(checkpoint_id, str) and checkpoint_id.strip():
                                checkpoint_state["latest_checkpoint_id"] = checkpoint_id.strip()
                            run_id = checkpoint.get("run_id")
                            if isinstance(run_id, str) and run_id.strip():
                                checkpoint_state["run_id"] = run_id.strip()
                        snapshot_id = snapshot_meta.get("snapshot_id")
                        if isinstance(snapshot_id, str) and snapshot_id.strip():
                            checkpoint_state["latest_snapshot_id"] = snapshot_id.strip()

                    undo_meta = result.get("undo")
                    if isinstance(undo_meta, dict):
                        undo_snapshot_id = undo_meta.get("restored_snapshot_id")
                        if isinstance(undo_snapshot_id, str) and undo_snapshot_id.strip():
                            checkpoint_state["latest_snapshot_id"] = undo_snapshot_id.strip()
                        checkpoint = undo_meta.get("checkpoint")
                        if isinstance(checkpoint, dict):
                            checkpoint_id = checkpoint.get("checkpoint_id")
                            if isinstance(checkpoint_id, str) and checkpoint_id.strip():
                                checkpoint_state["latest_checkpoint_id"] = checkpoint_id.strip()
                state = append_event(
                    state,
                    kind="tools",
                    data={
                        "count": len(calls),
                        "tools": [str(c.get("tool")) for c in calls],
                        "lane": route_metadata.get("lane", "interactive"),
                    },
                )
        except Exception as exc:
            log.error(
                "executor_step_failed",
                error=str(exc),
                step_id=step.get("id"),
            )
            tool_results.append(
                {
                    "tool": "batch_execute",
                    "ok": False,
                    "exit_code": 1,
                    "stdout": "",
                    "stderr": str(exc),
                    "diagnostics": [],
                    "timing_ms": 0,
                    "artifacts": {"error": "executor_failed"},
                    "route": tool_routing_metadata(state, stage="executor"),
                }
            )
            stop_execution = True
    budgets["tool_calls_used"] = tool_calls_used
    budgets["tool_calls_limit"] = max_tool_calls
    budgets["patch_bytes_limit"] = max_patch_bytes
    out = {**state, "tool_results": tool_results, "_checkpoint": checkpoint_state, "budgets": budgets}
    out = append_event(out, kind="node", data={"name": "executor", "phase": "end"})
    return prune_pre_verification_history(out)
