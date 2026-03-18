from __future__ import annotations

from typing import Any

from lg_orch.logging import get_logger
from lg_orch.trace import append_event


def _coerce_handoff(raw: object) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None

    producer = str(raw.get("producer", "")).strip()
    consumer = str(raw.get("consumer", "")).strip()
    objective = str(raw.get("objective", "")).strip()
    if not producer or not consumer or not objective:
        return None

    file_scope_raw = raw.get("file_scope", [])
    file_scope = [
        str(path).strip()
        for path in file_scope_raw
        if isinstance(file_scope_raw, list) and str(path).strip()
    ]
    evidence_raw = raw.get("evidence", [])
    evidence = [dict(entry) for entry in evidence_raw if isinstance(entry, dict)] if isinstance(evidence_raw, list) else []
    constraints_raw = raw.get("constraints", [])
    constraints = [
        str(item).strip() for item in constraints_raw if isinstance(item, str) and item.strip()
    ] if isinstance(constraints_raw, list) else []
    acceptance_checks_raw = raw.get("acceptance_checks", [])
    acceptance_checks = [
        str(item).strip()
        for item in acceptance_checks_raw
        if isinstance(item, str) and item.strip()
    ] if isinstance(acceptance_checks_raw, list) else []
    provenance_raw = raw.get("provenance", [])
    provenance = [
        str(item).strip() for item in provenance_raw if isinstance(item, str) and item.strip()
    ] if isinstance(provenance_raw, list) else []
    retry_budget_raw = raw.get("retry_budget", 1)
    retry_budget = retry_budget_raw if isinstance(retry_budget_raw, int) and retry_budget_raw >= 0 else 1

    return {
        "producer": producer,
        "consumer": consumer,
        "objective": objective,
        "file_scope": file_scope,
        "evidence": evidence,
        "constraints": constraints,
        "acceptance_checks": acceptance_checks,
        "retry_budget": retry_budget,
        "provenance": provenance,
    }


def _first_step(plan: dict[str, Any]) -> dict[str, Any] | None:
    steps_raw = plan.get("steps", [])
    if not isinstance(steps_raw, list):
        return None
    for step in steps_raw:
        if isinstance(step, dict):
            return step
    return None


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        normalized = item.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
    return out


def coder(state: dict[str, Any]) -> dict[str, Any]:
    log = get_logger()
    state = append_event(state, kind="node", data={"name": "coder", "phase": "start"})

    plan_raw = state.get("plan", {})
    plan = dict(plan_raw) if isinstance(plan_raw, dict) else {}
    step = _first_step(plan)
    if step is None:
        return append_event(state, kind="node", data={"name": "coder", "phase": "end", "handoff": "none"})

    active_handoff = _coerce_handoff(state.get("active_handoff"))
    step_handoff = _coerce_handoff(step.get("handoff"))
    source_handoff = active_handoff if active_handoff and active_handoff.get("consumer") == "coder" else step_handoff

    if source_handoff is None or source_handoff.get("consumer") != "coder":
        return append_event(state, kind="node", data={"name": "coder", "phase": "end", "handoff": "pass_through"})

    step_id = str(step.get("id", "step")).strip() or "step"
    step_description = str(step.get("description", "")).strip()
    expected_outcome = str(step.get("expected_outcome", "")).strip()
    files_touched_raw = step.get("files_touched", [])
    files_touched = [
        str(path).strip() for path in files_touched_raw if isinstance(files_touched_raw, list) and str(path).strip()
    ]

    evidence = list(source_handoff.get("evidence", []))
    if step_description:
        evidence.append({"kind": "plan_step", "ref": step_id, "detail": step_description})
    if expected_outcome:
        evidence.append({"kind": "expected_outcome", "ref": step_id, "detail": expected_outcome})

    constraints = list(source_handoff.get("constraints", []))
    constraints.append("Execute only the current step's planned tools.")

    acceptance_checks = list(source_handoff.get("acceptance_checks", []))
    if expected_outcome and expected_outcome not in acceptance_checks:
        acceptance_checks.append(expected_outcome)

    next_handoff = {
        "producer": "coder",
        "consumer": "executor",
        "objective": "Execute the bounded tool sequence prepared by the coder for the current step.",
        "file_scope": _dedupe(list(source_handoff.get("file_scope", [])) + files_touched),
        "evidence": evidence,
        "constraints": _dedupe(constraints),
        "acceptance_checks": _dedupe(acceptance_checks),
        "retry_budget": int(source_handoff.get("retry_budget", 1) or 0),
        "provenance": _dedupe(list(source_handoff.get("provenance", [])) + [f"coder:{step_id}"]),
    }

    log.info("coder_handoff_prepared", step_id=step_id, file_scope=next_handoff["file_scope"])
    out = {**state, "active_handoff": next_handoff, "retry_target": None}
    return append_event(
        out,
        kind="node",
        data={"name": "coder", "phase": "end", "handoff_consumer": "executor", "step_id": step_id},
    )
