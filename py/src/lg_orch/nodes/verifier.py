from __future__ import annotations

import re
from hashlib import sha256
from typing import Any

from lg_orch.logging import get_logger
from lg_orch.memory import ensure_history_policy, prune_post_verification_history
from lg_orch.model_routing import record_model_route, tool_routing_metadata
from lg_orch.state import RetryTarget, VerificationCheck, VerifierReport
from lg_orch.tools import RunnerClient
from lg_orch.trace import append_event

_ARCH_MISMATCH_HINTS = (
    "no such file",
    "cannot find module",
    "module not found",
    "unresolved import",
    "could not find",
    "cannot find file",
    "file not found",
    "path escapes root",
    "read denied",
)


def _extract_diagnostics(result: dict[str, Any]) -> list[dict[str, Any]]:
    direct = result.get("diagnostics", [])
    if isinstance(direct, list):
        return [d for d in direct if isinstance(d, dict)]
    artifacts = result.get("artifacts", {})
    if isinstance(artifacts, dict):
        nested = artifacts.get("diagnostics", [])
        if isinstance(nested, list):
            return [d for d in nested if isinstance(d, dict)]
    return []


def _diagnostic_summary(d: dict[str, Any]) -> str:
    file_ = str(d.get("file", "")).strip()
    line = d.get("line")
    col = d.get("column")
    code = str(d.get("code", "")).strip()
    message = str(d.get("message", "")).strip()

    location = ""
    if file_:
        location = file_
        if isinstance(line, int):
            location = f"{location}:{line}"
            if isinstance(col, int):
                location = f"{location}:{col}"

    code_prefix = f"[{code}] " if code else ""
    if location and message:
        return f"{location} {code_prefix}{message}".strip()
    if message:
        return f"{code_prefix}{message}".strip()
    return ""


def _first_nonempty_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def _failure_fingerprint(result: dict[str, Any], diagnostics: list[dict[str, Any]]) -> str:
    if diagnostics and isinstance(diagnostics[0], dict):
        direct = str(diagnostics[0].get("fingerprint", "")).strip()
        if direct:
            return direct

    artifacts = result.get("artifacts", {})
    artifacts_dict = dict(artifacts) if isinstance(artifacts, dict) else {}
    seed = "|".join(
        [
            str(result.get("tool", "")).strip(),
            str(artifacts_dict.get("error", "")).strip(),
            _diagnostic_summary(diagnostics[0]) if diagnostics else "",
            _first_nonempty_line(str(result.get("stderr", ""))),
        ]
    )
    return sha256(seed.encode("utf-8", errors="replace")).hexdigest()[:16]


def _is_architecture_mismatch(
    *,
    tool: str,
    diagnostics: list[dict[str, Any]],
    stderr: str,
    artifacts: dict[str, Any],
) -> bool:
    tool_name = tool.strip().lower()
    if tool_name == "read_file":
        return True

    err_tag = str(artifacts.get("error", "")).strip().lower()
    if err_tag in {"read_denied", "path escapes root"}:
        return True

    messages: list[str] = []
    for d in diagnostics:
        messages.append(_diagnostic_summary(d).lower())
        code = str(d.get("code", "")).strip().lower()
        if code in {"e0432", "e0583", "f821"}:
            return True

    if stderr:
        messages.append(stderr.lower())

    joined = "\n".join(messages)
    if any(hint in joined for hint in _ARCH_MISMATCH_HINTS):
        return True

    return bool(
        re.search(r"\bmissing\b", joined) and re.search(r"\bmodule\b|\bfile\b", joined)
    )


def _classify_retry(
    tool_results: list[dict[str, Any]],
    *,
    current_loop: int,
) -> tuple[dict[str, Any], str]:
    for result in reversed(tool_results):
        if bool(result.get("ok", False)):
            continue
        tool = str(result.get("tool", ""))
        stderr = str(result.get("stderr", ""))
        artifacts = result.get("artifacts", {})
        if not isinstance(artifacts, dict):
            artifacts = {}
        diagnostics = _extract_diagnostics(result)
        fingerprint = _failure_fingerprint(result, diagnostics)
        if _is_architecture_mismatch(
            tool=tool,
            diagnostics=diagnostics,
            stderr=stderr,
            artifacts=artifacts,
        ):
            return (
                {
                    "failure_class": "architecture_mismatch",
                    "failure_fingerprint": fingerprint,
                    "rationale": "tool failure indicates repository/context shape mismatch",
                    "retry_target": "context_builder",
                    "context_scope": "full_reset",
                    "plan_action": "discard_reset",
                },
                "architecture_mismatch_detected",
            )

        error_tag = str(artifacts.get("error", "")).strip().lower()
        if error_tag in {"tool_call_budget_exceeded", "patch_size_budget_exceeded"}:
            return (
                {
                    "failure_class": "budget_exceeded",
                    "failure_fingerprint": fingerprint,
                    "rationale": "the bounded execution budget blocked the current plan",
                    "retry_target": "planner",
                    "context_scope": "working_set",
                    "plan_action": "amend",
                },
                error_tag,
            )

        if current_loop >= 2:
            return (
                {
                    "failure_class": "repeated_verification_failure",
                    "failure_fingerprint": fingerprint,
                    "rationale": "repeated failures require recovery routing before replanning",
                    "retry_target": "router",
                    "context_scope": "working_set",
                    "plan_action": "amend",
                },
                "repeated_verification_failure",
            )

        return (
            {
                "failure_class": "verification_failed",
                "failure_fingerprint": fingerprint,
                "rationale": "verification found a failing tool result",
                "retry_target": "planner",
                "context_scope": "working_set",
                "plan_action": "keep",
            },
            "verification_failed",
        )
    return (
        {
            "failure_class": "verification_failed",
            "failure_fingerprint": "verification_failed",
            "rationale": "verification reported a failure without a detailed tool result",
            "retry_target": "planner",
            "context_scope": "working_set",
            "plan_action": "keep",
        },
        "verification_failed",
    )


def _build_checks(tool_results: list[dict[str, Any]]) -> list[VerificationCheck]:
    checks: list[VerificationCheck] = []
    for idx, result in enumerate(tool_results):
        if bool(result.get("ok", False)):
            continue
        tool = str(result.get("tool", "unknown"))
        exit_code_raw = result.get("exit_code", 1)
        try:
            exit_code = int(exit_code_raw)
        except (TypeError, ValueError):
            exit_code = 1

        diagnostics = _extract_diagnostics(result)
        summary = ""
        if diagnostics:
            summary = _diagnostic_summary(diagnostics[0])
        if not summary:
            summary = _first_nonempty_line(str(result.get("stderr", "")))

        checks.append(
            VerificationCheck(
                name=f"tool_failure_{idx + 1}",
                ok=False,
                tool=tool,
                exit_code=exit_code,
                summary=summary,
            )
        )
    return checks


def _validate_base_url(url: str) -> bool:
    return url.startswith("http://") or url.startswith("https://")


def _run_verification_calls(state: dict[str, Any], tool_results: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if bool(state.get("_runner_enabled", True)) is False:
        return tool_results, dict(state.get("budgets", {})) if isinstance(state.get("budgets", {}), dict) else {}

    plan_raw = state.get("plan", {})
    plan = dict(plan_raw) if isinstance(plan_raw, dict) else {}
    verification_calls_raw = plan.get("verification", [])
    verification_calls = [entry for entry in verification_calls_raw if isinstance(entry, dict)]
    budgets_raw = state.get("budgets", {})
    budgets = dict(budgets_raw) if isinstance(budgets_raw, dict) else {}
    if not verification_calls:
        return tool_results, budgets

    runner_base_url = str(state.get("_runner_base_url", "http://127.0.0.1:8088"))
    if not _validate_base_url(runner_base_url):
        return tool_results + [
            {
                "tool": "verification_batch",
                "ok": False,
                "exit_code": 1,
                "stdout": "",
                "stderr": "invalid runner base url",
                "diagnostics": [],
                "timing_ms": 0,
                "artifacts": {"error": "invalid_base_url"},
                "route": tool_routing_metadata(state, stage="verifier"),
            }
        ], budgets

    tool_calls_limit = int(state.get("_budget_max_tool_calls_per_loop", 0) or 0)
    tool_calls_used = int(budgets.get("tool_calls_used", 0) or 0)
    route_metadata = tool_routing_metadata(state, stage="verifier")
    if tool_calls_limit > 0 and tool_calls_used + len(verification_calls) > tool_calls_limit:
        return tool_results + [
            {
                "tool": "verification_batch",
                "ok": False,
                "exit_code": 1,
                "stdout": "",
                "stderr": (
                    f"tool-call budget exceeded during verification: used={tool_calls_used} "
                    f"planned={len(verification_calls)} limit={tool_calls_limit}"
                ),
                "diagnostics": [],
                "timing_ms": 0,
                "artifacts": {"error": "tool_call_budget_exceeded"},
                "route": route_metadata,
            }
        ], budgets

    checkpoint_state_raw = state.get("_checkpoint", {})
    checkpoint_state = dict(checkpoint_state_raw) if isinstance(checkpoint_state_raw, dict) else {}
    calls: list[dict[str, Any]] = []
    for call in verification_calls:
        input_payload = dict(call.get("input", {}))
        input_payload["_route"] = route_metadata
        if checkpoint_state:
            input_payload["_checkpoint"] = checkpoint_state
        calls.append({"tool": str(call.get("tool", "")), "input": input_payload})

    api_key = state.get("_runner_api_key")
    api_key_s = str(api_key).strip() if api_key is not None else None
    client = RunnerClient(base_url=runner_base_url, api_key=api_key_s)
    try:
        batch_results = client.batch_execute_tools(calls=calls)
    finally:
        client.close()
    budgets["tool_calls_used"] = tool_calls_used + len(calls)
    return tool_results + batch_results, budgets


def verifier(state: dict[str, Any]) -> dict[str, Any]:
    log = get_logger()
    state = ensure_history_policy(state)
    state = record_model_route(
        state,
        node_name="verifier",
        task_class="lint_reflection",
        model_slot="router",
    )
    state = append_event(state, kind="node", data={"name": "verifier", "phase": "start"})

    tool_results_raw = state.get("tool_results", [])
    tool_results: list[dict[str, Any]]
    if isinstance(tool_results_raw, list):
        tool_results = [r for r in tool_results_raw if isinstance(r, dict)]
    else:
        tool_results = []

    try:
        tool_results, budgets = _run_verification_calls(state, tool_results)
    except Exception as exc:
        log.error("verifier_execution_failed", error=str(exc))
        budgets_raw = state.get("budgets", {})
        budgets = dict(budgets_raw) if isinstance(budgets_raw, dict) else {}
        tool_results.append(
            {
                "tool": "verification_batch",
                "ok": False,
                "exit_code": 1,
                "stdout": "",
                "stderr": str(exc),
                "diagnostics": [],
                "timing_ms": 0,
                "artifacts": {"error": "verifier_execution_failed"},
                "route": tool_routing_metadata(state, stage="verifier"),
            }
        )

    checks = _build_checks(tool_results)
    has_failures = len(checks) > 0
    discard_reason = ""
    current_loop_raw = state.get("budgets", {})
    current_loop_state = dict(current_loop_raw) if isinstance(current_loop_raw, dict) else {}
    current_loop = int(current_loop_state.get("current_loop", 0) or 0)

    try:
        if has_failures:
            recovery, discard_reason = _classify_retry(tool_results, current_loop=current_loop)
            loop_summary = f"{recovery['failure_class']}: {checks[0].summary if checks else discard_reason}"
            report = VerifierReport(
                ok=False,
                checks=checks,
                retry_target=recovery["retry_target"],
                plan_action=recovery["plan_action"],
                failure_class=recovery["failure_class"],
                failure_fingerprint=recovery["failure_fingerprint"],
                recovery=recovery,
                loop_summary=loop_summary,
            ).model_dump()
        else:
            report = VerifierReport(
                ok=True,
                checks=[],
                retry_target=None,
                plan_action="keep",
                failure_class="",
                failure_fingerprint="",
                recovery=None,
                loop_summary="verification_passed",
            ).model_dump()
    except Exception as exc:
        log.error("verifier_failed", error=str(exc))
        report = {
            "ok": False,
            "checks": [],
            "retry_target": "planner",
            "plan_action": "keep",
            "failure_class": "verifier_failure",
            "failure_fingerprint": "verifier_failure",
            "recovery": None,
            "loop_summary": "verifier failed to classify results",
        }

    ok = bool(report.get("ok", False))
    loop_summaries_raw = state.get("loop_summaries", [])
    loop_summaries = list(loop_summaries_raw) if isinstance(loop_summaries_raw, list) else []
    if not ok:
        loop_summaries.append(
            {
                "loop": current_loop,
                "failure_class": report.get("failure_class", ""),
                "failure_fingerprint": report.get("failure_fingerprint", ""),
                "summary": report.get("loop_summary", ""),
                "recovery": report.get("recovery"),
            }
        )
        loop_summaries = loop_summaries[-5:]

    out: dict[str, Any] = {
        **state,
        "tool_results": tool_results,
        "verification": report,
        "budgets": budgets,
        "retry_target": report.get("retry_target"),
        "loop_summaries": loop_summaries,
    }
    if ok:
        out["context_reset_requested"] = False
        out["plan_discarded"] = False
        out["plan_discard_reason"] = ""
    elif report.get("plan_action") == "discard_reset":
        out["plan"] = None
        out["context_reset_requested"] = True
        out["plan_discarded"] = True
        out["plan_discard_reason"] = discard_reason or "architecture_mismatch_detected"

    out = append_event(
        out,
        kind="node",
        data={
            "name": "verifier",
            "phase": "end",
            "ok": ok,
            "failure_class": report.get("failure_class", ""),
        },
    )
    return prune_post_verification_history(out)
