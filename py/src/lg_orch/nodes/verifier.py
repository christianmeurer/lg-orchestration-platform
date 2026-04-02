# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Christian Meurer — https://github.com/christianmeurer/Lula
from __future__ import annotations

import json
import re
from hashlib import sha256
from pathlib import Path
from typing import Any

import jsonschema  # type: ignore[import-untyped]
from pydantic import BaseModel

from lg_orch.logging import get_logger
from lg_orch.memory import (
    _state_to_dict,
    ensure_history_policy,
    get_compression_summary,
    prune_post_verification_history,
)
from lg_orch.model_routing import record_model_route, tool_routing_metadata
from lg_orch.nodes._utils import validate_base_url as _validate_base_url_fn
from lg_orch.state import (
    AgentHandoff,
    HandoffEvidence,
    RecoveryAction,
    RecoveryPacket,
    VerificationCheck,
    VerifierReport,
)
from lg_orch.tools import RunnerClient
from lg_orch.trace import append_event

_VERIFIER_SCHEMA_PATH = (
    Path(__file__).parent.parent.parent.parent.parent / "schemas" / "verifier_report.schema.json"
)


def _load_verifier_schema() -> dict[str, Any]:
    try:
        return json.loads(_VERIFIER_SCHEMA_PATH.read_text(encoding="utf-8"))  # type: ignore[no-any-return]
    except Exception:
        return {}


VERIFIER_SCHEMA: dict[str, Any] = _load_verifier_schema()

_TEST_FAILURE_HINTS = (
    "assert",
    "assertion",
    "test_",
    "tests/",
    "test failed",
    "expected",
    "got",
    "does not match",
    "mismatch",
    "expected value",
    "pytest",
    "unittest",
    "FAILED",
    "test suite",
)

_PATCH_APPLIED_HINTS = (
    "apply_patch",
    "patch_applied",
    "file_written",
    "write_file",
)

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


def _validate_base_url(url: str) -> bool:
    try:
        _validate_base_url_fn(url, "runner_base_url")
        return True
    except ValueError:
        return False


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

    return bool(re.search(r"\bmissing\b", joined) and re.search(r"\bmodule\b|\bfile\b", joined))


def _is_test_failure_post_change(
    *,
    tool: str,
    diagnostics: list[dict[str, Any]],
    stderr: str,
    stdout: str,
    artifacts: dict[str, Any],
    tool_results: list[dict[str, Any]],
) -> bool:
    """
    Returns True when:
    1. The failing tool looks like a test runner (run_tests, pytest, cargo test, etc.)
    2. AND a patch was applied earlier in the same loop (apply_patch succeeded in tool_results)
    """
    tool_lower = tool.strip().lower()
    is_test_tool = (
        tool_lower in {"run_tests", "pytest", "cargo_test", "test_runner"} or "test" in tool_lower
    )
    if not is_test_tool:
        all_text = " ".join(
            [_diagnostic_summary(d) for d in diagnostics] + [stderr, stdout]
        ).lower()
        is_test_tool = any(hint in all_text for hint in _TEST_FAILURE_HINTS)

    if not is_test_tool:
        return False

    # Check that a patch was applied and succeeded earlier in this loop
    patch_applied = any(
        str(r.get("tool", "")).strip().lower() in {"apply_patch", "write_file"}
        and bool(r.get("ok", False))
        for r in tool_results
    )
    return patch_applied


def _requires_formal_verification(
    state: dict[str, Any], tool_results: list[dict[str, Any]]
) -> list[str]:
    if not state.get("_vericoding_enabled", False):
        return []

    extensions = tuple(state.get("_vericoding_extensions", [".rs"]))
    files_to_verify: list[str] = []

    for result in tool_results:
        if str(result.get("tool", "")) == "apply_patch" and result.get("ok"):
            input_payload = result.get("input", {})
            if isinstance(input_payload, dict):
                changes = input_payload.get("changes", [])
                for change in changes:
                    if isinstance(change, dict):
                        path = str(change.get("path", ""))
                        if path.endswith(extensions) and path not in files_to_verify:
                            files_to_verify.append(path)

    return files_to_verify


def _run_formal_verification(
    state: dict[str, Any], files_to_verify: list[str], route_metadata: dict[str, Any]
) -> dict[str, Any] | None:
    if not files_to_verify:
        return None

    runner_base_url = str(state.get("_runner_base_url", "http://127.0.0.1:8088"))
    if not _validate_base_url(runner_base_url):
        return {
            "tool": "formal_verification",
            "ok": False,
            "exit_code": 1,
            "stdout": "",
            "stderr": "invalid runner base url for formal verification",
            "diagnostics": [],
            "artifacts": {"error": "invalid_base_url"},
            "route": route_metadata,
        }
    api_key = state.get("_runner_api_key")
    request_id = state.get("_request_id")
    api_key_s = str(api_key).strip() if api_key is not None else None
    request_id_s = str(request_id).strip() if request_id is not None else None

    from lg_orch.tools import RunnerClient

    client = RunnerClient(base_url=runner_base_url, api_key=api_key_s, request_id=request_id_s)

    try:
        args = ["test", "--all", "--features", "verify"]

        call: dict[str, Any] = {
            "tool": "exec",
            "input": {"cmd": "cargo", "args": args, "timeout_s": 120, "_route": route_metadata},
        }

        results = client.batch_execute_tools(calls=[call])
        if results:
            result = results[0]
            if not result.get("ok"):
                return {
                    "tool": "formal_verification",
                    "ok": False,
                    "exit_code": result.get("exit_code", 1),
                    "stdout": result.get("stdout", ""),
                    "stderr": f"Formal Verification Failed:\n{result.get('stderr', '')}",
                    "diagnostics": result.get("diagnostics", []),
                    "artifacts": {"error": "formal_verification_failed", "files": files_to_verify},
                    "route": route_metadata,
                }
        return None
    except Exception as e:
        return {
            "tool": "formal_verification",
            "ok": False,
            "exit_code": 1,
            "stdout": "",
            "stderr": f"Failed to execute formal verification: {e!s}",
            "diagnostics": [],
            "artifacts": {"error": "formal_verification_execution_error"},
            "route": route_metadata,
        }
    finally:
        client.close()


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

        if error_tag == "formal_verification_failed":
            return (
                {
                    "failure_class": "formal_verification_failed",
                    "failure_fingerprint": fingerprint,
                    "rationale": (
                        "Symbolic proof checker rejected the implementation."
                        " The logic must be mathematically verified."
                    ),
                    "retry_target": "planner",
                    "context_scope": "working_set",
                    "plan_action": "amend",
                },
                "formal_verification_failed",
            )

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

        # Check for test failure post-change (Reflect phase)
        if _is_test_failure_post_change(
            tool=tool,
            diagnostics=diagnostics,
            stderr=stderr,
            stdout=str(result.get("stdout", "")),
            artifacts=artifacts,
            tool_results=tool_results,
        ):
            return (
                {
                    "failure_class": "test_failure_post_change",
                    "failure_fingerprint": fingerprint,
                    "rationale": (
                        "test failed after a patch was applied; coder should attempt"
                        " a localized repair before broader replanning"
                    ),
                    "retry_target": "coder",
                    "context_scope": "working_set",
                    "plan_action": "amend",
                },
                "test_failure_post_change",
            )

        patch_applied = any(
            str(entry.get("tool", "")).strip().lower() in {"apply_patch", "write_file"}
            and bool(entry.get("ok", False))
            for entry in tool_results
        )
        if patch_applied:
            return (
                {
                    "failure_class": "localized_verification_failure",
                    "failure_fingerprint": fingerprint,
                    "rationale": (
                        "verification failed after a bounded patch; coder should attempt"
                        " a localized repair before broader replanning"
                    ),
                    "retry_target": "coder",
                    "context_scope": "working_set",
                    "plan_action": "amend",
                },
                "localized_verification_failure",
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


def _recovery_action_payload(recovery: dict[str, Any]) -> dict[str, Any]:
    return RecoveryAction(
        failure_class=str(recovery.get("failure_class", "")).strip(),
        failure_fingerprint=str(recovery.get("failure_fingerprint", "")).strip(),
        rationale=str(recovery.get("rationale", "")).strip(),
        retry_target=str(recovery.get("retry_target", "planner")).strip() or "planner",  # type: ignore[arg-type]
        context_scope=str(recovery.get("context_scope", "working_set")).strip() or "working_set",  # type: ignore[arg-type]
        plan_action=str(recovery.get("plan_action", "keep")).strip() or "keep",  # type: ignore[arg-type]
    ).model_dump()


def _recovery_packet_payload(
    recovery: dict[str, Any],
    *,
    current_loop: int,
    loop_summary: str,
    last_check: str,
    discard_reason: str,
) -> dict[str, Any]:
    return RecoveryPacket(
        **_recovery_action_payload(recovery),
        loop=max(int(current_loop), 0),
        origin="verifier",
        summary=loop_summary,
        last_check=last_check,
        discard_reason=discard_reason,
    ).model_dump()


def _next_handoff_payload(
    state: dict[str, Any],
    *,
    report: dict[str, Any],
    checks: list[VerificationCheck],
    current_loop: int,
) -> dict[str, Any] | None:
    consumer = str(report.get("retry_target", "")).strip()
    if consumer not in {"planner", "coder", "context_builder", "router"}:
        return None

    request = str(state.get("request", "")).strip()
    failure_class = str(report.get("failure_class", "")).strip()
    summary = str(report.get("loop_summary", "")).strip()
    if not summary and checks:
        summary = str(checks[0].summary).strip()

    file_scope: list[str] = []
    active_handoff_raw = state.get("active_handoff", {})
    active_handoff = dict(active_handoff_raw) if isinstance(active_handoff_raw, dict) else {}
    active_scope_raw = active_handoff.get("file_scope", [])
    if isinstance(active_scope_raw, list):
        file_scope.extend(str(item).strip() for item in active_scope_raw if str(item).strip())

    plan_raw = state.get("plan", {})
    plan = dict(plan_raw) if isinstance(plan_raw, dict) else {}
    steps_raw = plan.get("steps", [])
    if isinstance(steps_raw, list):
        for step in steps_raw:
            if not isinstance(step, dict):
                continue
            touched_raw = step.get("files_touched", [])
            if isinstance(touched_raw, list):
                file_scope.extend(str(item).strip() for item in touched_raw if str(item).strip())

    deduped_scope: list[str] = []
    for item in file_scope:
        if item and item not in deduped_scope:
            deduped_scope.append(item)

    evidence: list[dict[str, Any]] = []
    if request:
        evidence.append(
            HandoffEvidence(kind="request", ref="user_request", detail=request).model_dump()
        )
    if summary:
        evidence.append(
            HandoffEvidence(
                kind="verification",
                ref=failure_class or "verification_failed",
                detail=summary,
            ).model_dump()
        )
    prior_objective = str(active_handoff.get("objective", "")).strip()
    if prior_objective:
        evidence.append(
            HandoffEvidence(
                kind="prior_handoff",
                ref=str(active_handoff.get("consumer", "")),
                detail=prior_objective,
            ).model_dump()
        )

    objective = "Replan the next bounded iteration using the latest verifier evidence."
    constraints = ["Prefer a bounded next step."]
    acceptance_checks = ["The next action addresses the verifier evidence."]
    if consumer == "coder":
        objective = (
            "Prepare a localized repair using the current plan and the latest verifier evidence."
        )
        constraints = [
            "Stay within the current file scope unless evidence proves"
            " a broader change is required.",
            "Prefer amending the existing bounded plan over broad replanning.",
        ]
        acceptance_checks = [
            "The localized failure is addressed without broadening scope unnecessarily."
        ]
    elif consumer == "context_builder":
        objective = "Rebuild repository context before the next bounded planning iteration."
        constraints = ["Discard stale working-set assumptions before replanning."]
        acceptance_checks = [
            "The next context snapshot resolves the detected architecture mismatch."
        ]
    elif consumer == "router":
        objective = "Re-route the task after repeated verification failures."
        constraints = ["Choose a stronger recovery topology before executing again."]
        acceptance_checks = ["The next route accounts for repeated failure evidence."]

    return AgentHandoff(
        producer="verifier",
        consumer=consumer,
        objective=objective,
        file_scope=deduped_scope,
        evidence=evidence,  # type: ignore[arg-type]
        constraints=constraints,
        acceptance_checks=acceptance_checks,
        retry_budget=1,
        provenance=[
            f"verifier:loop:{current_loop}",
            f"failure_class:{failure_class or 'verification_failed'}",
        ],
    ).model_dump()


def _loop_summary_entry(
    report: dict[str, Any],
    *,
    current_loop: int,
    acceptance_criteria: list[str] | None = None,
    acceptance_checks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    recovery_packet_raw = report.get("recovery_packet", {})
    recovery_packet = dict(recovery_packet_raw) if isinstance(recovery_packet_raw, dict) else {}
    criteria = acceptance_criteria if acceptance_criteria is not None else []
    checks = acceptance_checks if acceptance_checks is not None else []
    unmet_criteria = [
        str(entry.get("criterion", "")).strip()
        for entry in checks
        if isinstance(entry, dict) and not bool(entry.get("ok", False))
    ]
    return {
        "loop": current_loop,
        "failure_class": report.get("failure_class", ""),
        "failure_fingerprint": report.get("failure_fingerprint", ""),
        "retry_target": report.get("retry_target"),
        "plan_action": report.get("plan_action", "keep"),
        "context_scope": recovery_packet.get("context_scope", ""),
        "summary": report.get("loop_summary", ""),
        "last_check": recovery_packet.get("last_check", ""),
        "discard_reason": recovery_packet.get("discard_reason", ""),
        "recovery": report.get("recovery"),
        "recovery_packet": recovery_packet or None,
        "acceptance_criteria": criteria,
        "acceptance_unmet": unmet_criteria,
        "acceptance_ok": report.get("acceptance_ok", False),
    }


def _updated_recovery_facts(
    state: dict[str, Any], *, report: dict[str, Any], current_loop: int
) -> list[dict[str, Any]]:
    facts_raw = state.get("facts", [])
    facts = (
        [dict(entry) for entry in facts_raw if isinstance(entry, dict)]
        if isinstance(facts_raw, list)
        else []
    )
    recovery_packet_raw = report.get("recovery_packet", {})
    recovery_packet = dict(recovery_packet_raw) if isinstance(recovery_packet_raw, dict) else {}
    if not recovery_packet:
        return facts

    plan_action = str(report.get("plan_action", "keep")).strip() or "keep"
    salience = 6
    if plan_action == "discard_reset":
        salience = 10
    elif str(report.get("retry_target", "")).strip() == "router":
        salience = 8

    next_fact = {
        "kind": "recovery_fact",
        "loop": current_loop,
        "failure_class": str(report.get("failure_class", "")).strip(),
        "failure_fingerprint": str(report.get("failure_fingerprint", "")).strip(),
        "summary": str(report.get("loop_summary", "")).strip(),
        "last_check": str(recovery_packet.get("last_check", "")).strip(),
        "context_scope": str(recovery_packet.get("context_scope", "")).strip(),
        "retry_target": report.get("retry_target"),
        "plan_action": plan_action,
        "salience": salience,
    }

    fingerprint = str(next_fact.get("failure_fingerprint", "")).strip()
    updated = [
        entry for entry in facts if str(entry.get("failure_fingerprint", "")).strip() != fingerprint
    ]
    updated.append(next_fact)
    updated.sort(
        key=lambda entry: (
            int(entry.get("salience", 0) or 0),
            int(entry.get("loop", 0) or 0),
        ),
        reverse=True,
    )
    return updated[:12]


def _evaluate_acceptance_checks(
    state: dict[str, Any], *, tool_results: list[dict[str, Any]], checks: list[VerificationCheck]
) -> list[dict[str, Any]]:
    plan_raw = state.get("plan", {})
    plan = dict(plan_raw) if isinstance(plan_raw, dict) else {}
    criteria_raw = plan.get("acceptance_criteria", [])
    criteria = [
        str(entry).strip() for entry in criteria_raw if isinstance(entry, str) and entry.strip()
    ]
    if not criteria:
        return []

    repo_context_raw = state.get("repo_context", {})
    repo_context = dict(repo_context_raw) if isinstance(repo_context_raw, dict) else {}
    top_level = repo_context.get("top_level", [])
    has_repo_context = (
        bool(repo_context.get("repo_map"))
        or bool(isinstance(top_level, list) and top_level)
        or bool(repo_context.get("structural_ast_map"))
        or bool(repo_context.get("semantic_hits"))
    )
    plan_steps_raw = plan.get("steps", [])
    has_plan_steps = bool(isinstance(plan_steps_raw, list) and plan_steps_raw)
    successful_tool_results = any(bool(result.get("ok", False)) for result in tool_results)
    verification_passed = len(checks) == 0
    acceptance_checks: list[dict[str, Any]] = []
    for criterion in criteria:
        lower = criterion.lower()
        ok = verification_passed
        detail = "verification_passed" if verification_passed else "verification_failed"
        if "context" in lower:
            ok = has_repo_context
            detail = "repo_context_available" if ok else "repo_context_missing"
        elif "bounded" in lower or "next step" in lower:
            ok = has_plan_steps
            detail = "bounded_plan_available" if ok else "bounded_plan_missing"
        elif "request" in lower or "answered" in lower or "executed" in lower:
            ok = verification_passed and (successful_tool_results or has_plan_steps)
            detail = "request_path_available" if ok else "request_path_incomplete"
        acceptance_checks.append({"criterion": criterion, "ok": ok, "detail": detail})
    return acceptance_checks


def _acceptance_failure(acceptance_checks: list[dict[str, Any]]) -> tuple[dict[str, Any], str, str]:
    unmet = [
        entry
        for entry in acceptance_checks
        if isinstance(entry, dict) and not bool(entry.get("ok", False))
    ]
    if not unmet:
        return {}, "", ""

    summary = (
        str(unmet[0].get("criterion", "acceptance criteria unmet")).strip()
        or "acceptance criteria unmet"
    )
    fingerprint_seed = "|".join(str(entry.get("criterion", "")).strip() for entry in unmet)
    fingerprint = sha256(fingerprint_seed.encode("utf-8", errors="replace")).hexdigest()[:16]
    recovery = {
        "failure_class": "acceptance_criteria_unmet",
        "failure_fingerprint": fingerprint,
        "rationale": "verification passed but acceptance criteria are not yet satisfied",
        "retry_target": "planner",
        "context_scope": "working_set",
        "plan_action": "amend",
    }
    return recovery, "acceptance_criteria_unmet", summary


def _diagnostics_telemetry_entries(
    tool_results: list[dict[str, Any]],
    *,
    current_loop: int,
    report: dict[str, Any],
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    report_failure_class = str(report.get("failure_class", "")).strip()
    for result in tool_results:
        if bool(result.get("ok", False)):
            continue
        diagnostics = _extract_diagnostics(result)
        fingerprint = _failure_fingerprint(result, diagnostics)
        artifacts_raw = result.get("artifacts", {})
        artifacts = dict(artifacts_raw) if isinstance(artifacts_raw, dict) else {}
        summary = _diagnostic_summary(diagnostics[0]) if diagnostics else ""
        if not summary:
            summary = _first_nonempty_line(str(result.get("stderr", "")))
        entries.append(
            {
                "loop": current_loop,
                "tool": str(result.get("tool", "")).strip(),
                "failure_class": report_failure_class,
                "failure_fingerprint": fingerprint,
                "error": str(artifacts.get("error", "")).strip(),
                "summary": summary,
                "diagnostic_count": len(diagnostics),
            }
        )
    return entries


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


def _run_verification_calls(
    state: dict[str, Any], tool_results: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if bool(state.get("_runner_enabled", True)) is False:
        budgets_raw = state.get("budgets", {})
        return (
            tool_results,
            dict(budgets_raw) if isinstance(budgets_raw, dict) else {},
        )

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
        return [
            *tool_results,
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
            },
        ], budgets

    tool_calls_limit = int(state.get("_budget_max_tool_calls_per_loop", 0) or 0)
    tool_calls_used = int(budgets.get("tool_calls_used", 0) or 0)
    route_metadata = tool_routing_metadata(state, stage="verifier")
    if tool_calls_limit > 0 and tool_calls_used + len(verification_calls) > tool_calls_limit:
        return [
            *tool_results,
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
            },
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
    request_id = state.get("_request_id")
    request_id_s = str(request_id).strip() if request_id is not None else None
    client = RunnerClient(base_url=runner_base_url, api_key=api_key_s, request_id=request_id_s)
    try:
        batch_results = client.batch_execute_tools(calls=calls)
    finally:
        client.close()
    budgets["tool_calls_used"] = tool_calls_used + len(calls)
    return [*tool_results, *batch_results], budgets


def verifier(state: dict[str, Any] | BaseModel) -> dict[str, Any]:
    if isinstance(state, BaseModel):
        state = _state_to_dict(state)
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

    files_to_verify = _requires_formal_verification(state, tool_results)
    if files_to_verify and bool(state.get("_runner_enabled", True)):
        verification_failure = _run_formal_verification(
            state, files_to_verify, tool_routing_metadata(state, stage="verifier")
        )
        if verification_failure:
            tool_results.append(verification_failure)
            log.info("formal_verification_failed", files=files_to_verify)
        else:
            log.info("formal_verification_passed", files=files_to_verify)

    checks = _build_checks(tool_results)

    # --- GLEAN audit integration ---
    # If the executor recorded a GLEAN summary with blocking violations,
    # inject a synthetic check so the verifier fails accordingly.
    glean_blocking_count = 0
    trace_events_raw = state.get("_trace_events", [])
    if isinstance(trace_events_raw, list):
        for evt in trace_events_raw:
            if isinstance(evt, dict) and evt.get("kind") == "glean":
                glean_data = evt.get("data", {})
                if isinstance(glean_data, dict):
                    glean_blocking_count = int(glean_data.get("blocking_violations", 0) or 0)
                    if glean_blocking_count > 0:
                        checks.append(
                            VerificationCheck(
                                name="glean_blocking_violations",
                                ok=False,
                                tool="glean_audit",
                                exit_code=1,
                                summary=(
                                    f"GLEAN audit found {glean_blocking_count}"
                                    f" blocking violation(s)"
                                ),
                            )
                        )

    has_failures = len(checks) > 0
    discard_reason = ""
    current_loop_raw = state.get("budgets", {})
    current_loop_state = dict(current_loop_raw) if isinstance(current_loop_raw, dict) else {}
    current_loop = int(current_loop_state.get("current_loop", 0) or 0)

    try:
        acceptance_checks = _evaluate_acceptance_checks(
            state, tool_results=tool_results, checks=checks
        )
        acceptance_ok = all(bool(entry.get("ok", False)) for entry in acceptance_checks)
        if has_failures:
            recovery, discard_reason = _classify_retry(tool_results, current_loop=current_loop)
            recovery_action = _recovery_action_payload(recovery)
            last_check = checks[0].summary if checks else discard_reason
            loop_summary = f"{recovery_action['failure_class']}: {last_check}"
            recovery_packet = _recovery_packet_payload(
                recovery_action,
                current_loop=current_loop,
                loop_summary=loop_summary,
                last_check=last_check,
                discard_reason=discard_reason,
            )
            next_handoff = _next_handoff_payload(
                state,
                report={
                    "retry_target": recovery_action["retry_target"],
                    "failure_class": recovery_action["failure_class"],
                    "loop_summary": loop_summary,
                },
                checks=checks,
                current_loop=current_loop,
            )
            report = VerifierReport(
                ok=False,
                checks=checks,
                acceptance_ok=False,
                acceptance_checks=acceptance_checks,
                retry_target=recovery_action["retry_target"],
                plan_action=recovery_action["plan_action"],
                failure_class=recovery_action["failure_class"],
                failure_fingerprint=recovery_action["failure_fingerprint"],
                recovery=recovery_action,  # type: ignore[arg-type]
                recovery_packet=recovery_packet,  # type: ignore[arg-type]
                next_handoff=next_handoff,  # type: ignore[arg-type]
                loop_summary=loop_summary,
            ).model_dump()
        elif not acceptance_ok:
            recovery, discard_reason, last_check = _acceptance_failure(acceptance_checks)
            recovery_action = _recovery_action_payload(recovery)
            loop_summary = f"{recovery_action['failure_class']}: {last_check}"
            recovery_packet = _recovery_packet_payload(
                recovery_action,
                current_loop=current_loop,
                loop_summary=loop_summary,
                last_check=last_check,
                discard_reason=discard_reason,
            )
            next_handoff = _next_handoff_payload(
                state,
                report={
                    "retry_target": recovery_action["retry_target"],
                    "failure_class": recovery_action["failure_class"],
                    "loop_summary": loop_summary,
                },
                checks=[],
                current_loop=current_loop,
            )
            report = VerifierReport(
                ok=False,
                checks=[],
                acceptance_ok=False,
                acceptance_checks=acceptance_checks,
                retry_target=recovery_action["retry_target"],
                plan_action=recovery_action["plan_action"],
                failure_class=recovery_action["failure_class"],
                failure_fingerprint=recovery_action["failure_fingerprint"],
                recovery=recovery_action,  # type: ignore[arg-type]
                recovery_packet=recovery_packet,  # type: ignore[arg-type]
                next_handoff=next_handoff,  # type: ignore[arg-type]
                loop_summary=loop_summary,
            ).model_dump()
        else:
            report = VerifierReport(
                ok=True,
                checks=[],
                acceptance_ok=True,
                acceptance_checks=acceptance_checks,
                retry_target=None,
                plan_action="keep",
                failure_class="",
                failure_fingerprint="",
                recovery=None,
                recovery_packet=None,
                loop_summary="verification_passed",
            ).model_dump()
    except Exception as exc:
        log.error("verifier_failed", error=str(exc))
        report = {
            "ok": False,
            "checks": [],
            "acceptance_ok": False,
            "acceptance_checks": [],
            "retry_target": "planner",
            "plan_action": "keep",
            "failure_class": "verifier_failure",
            "failure_fingerprint": "verifier_failure",
            "recovery": None,
            "recovery_packet": None,
            "next_handoff": None,
            "loop_summary": "verifier failed to classify results",
        }

    if VERIFIER_SCHEMA:
        try:
            # In JSON Schema draft 2020-12, $ref alongside type:["object","null"]
            # applies $ref constraints even when the value is null, causing false
            # failures on nullable object fields. Strip only the three nullable
            # $ref fields when they are None so they are absent (= valid) from
            # the schema's perspective. Required scalar fields (retry_target, etc.)
            # are kept even when null because the schema allows null for those.
            _nullable_ref_keys = {"recovery", "recovery_packet", "next_handoff"}
            report_for_validation = {
                k: v for k, v in report.items() if not (k in _nullable_ref_keys and v is None)
            }
            jsonschema.validate(instance=report_for_validation, schema=VERIFIER_SCHEMA)
        except jsonschema.ValidationError as ve:
            log.warning("verifier_schema_validation_failed", error=str(ve.message))
            report = {
                "ok": False,
                "checks": [],
                "acceptance_ok": False,
                "acceptance_checks": [],
                "retry_target": "planner",
                "plan_action": "keep",
                "failure_class": "schema_validation_failed",
                "failure_fingerprint": "schema_validation_failed",
                "recovery": None,
                "recovery_packet": None,
                "next_handoff": None,
                "loop_summary": "schema_validation_failed",
            }

    ok = bool(report.get("ok", False))
    loop_summaries_raw = state.get("loop_summaries", [])
    loop_summaries = list(loop_summaries_raw) if isinstance(loop_summaries_raw, list) else []
    recovery_packet_raw = report.get("recovery_packet", {})
    recovery_packet = dict(recovery_packet_raw) if isinstance(recovery_packet_raw, dict) else None  # type: ignore[assignment]
    plan_raw = state.get("plan", {})
    plan = dict(plan_raw) if isinstance(plan_raw, dict) else {}
    acceptance_criteria_raw = plan.get("acceptance_criteria", [])
    acceptance_criteria = (
        [str(c).strip() for c in acceptance_criteria_raw if str(c).strip()]
        if isinstance(acceptance_criteria_raw, list)
        else []
    )
    acceptance_checks_raw = report.get("acceptance_checks", [])
    acceptance_checks = (
        [dict(c) for c in acceptance_checks_raw if isinstance(c, dict)]
        if isinstance(acceptance_checks_raw, list)
        else []
    )

    facts = state.get("facts", [])
    telemetry = state.get("telemetry", {})
    if not ok:
        loop_summaries.append(
            _loop_summary_entry(
                report,
                current_loop=current_loop,
                acceptance_criteria=acceptance_criteria,
                acceptance_checks=acceptance_checks,
            )
        )
        loop_summaries = loop_summaries[-5:]
        facts = _updated_recovery_facts(state, report=report, current_loop=current_loop)
        telemetry_raw = state.get("telemetry", {})
        telemetry = dict(telemetry_raw) if isinstance(telemetry_raw, dict) else {}
        diagnostics_raw = telemetry.get("diagnostics", [])
        diagnostics_telemetry = list(diagnostics_raw) if isinstance(diagnostics_raw, list) else []
        diagnostics_telemetry.extend(
            _diagnostics_telemetry_entries(tool_results, current_loop=current_loop, report=report)
        )
        telemetry["diagnostics"] = diagnostics_telemetry[-20:]
        telemetry["compression_summary"] = get_compression_summary(state)

    out: dict[str, Any] = {
        **state,
        "tool_results": tool_results,
        "verification": report,
        "budgets": budgets,
        "active_handoff": report.get("next_handoff"),
        "retry_target": report.get("retry_target"),
        "facts": facts,
        "recovery_packet": recovery_packet,
        "loop_summaries": loop_summaries,
        "telemetry": telemetry,
    }
    if ok:
        out["active_handoff"] = None
        out["recovery_packet"] = None
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
