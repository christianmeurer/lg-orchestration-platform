"""Pure memory-ranking helpers: semantic memory scoring and procedural memory recall."""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lg_orch.logging import get_logger

_WORD_RE = re.compile(r"[a-z0-9']+")
_FILE_HINT_RE = re.compile(r"(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+\.[A-Za-z0-9_.-]+")


def _dedupe_strings(values: list[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        normalized = value.strip()
        if normalized and normalized not in out:
            out.append(normalized)
    return out


def _rank_semantic_memories(request: str, repo_context: dict[str, Any], *, limit: int = 4) -> list[dict[str, Any]]:
    semantic_memories_raw = repo_context.get("semantic_memories", [])
    semantic_memories = (
        [dict(entry) for entry in semantic_memories_raw if isinstance(entry, dict)]
        if isinstance(semantic_memories_raw, list)
        else []
    )
    if not semantic_memories:
        return []

    request_tokens = set(_WORD_RE.findall(request.lower()))
    ranked: list[tuple[tuple[int, str, int], dict[str, Any]]] = []
    for idx, memory in enumerate(semantic_memories):
        summary = str(memory.get("summary", "")).strip()
        if not summary:
            continue
        kind = str(memory.get("kind", "")).strip()
        source = str(memory.get("source", "")).strip()
        created_at = str(memory.get("created_at", "")).strip()
        haystack_tokens = set(_WORD_RE.findall(f"{kind} {source} {summary}".lower()))
        overlap = len(request_tokens.intersection(haystack_tokens))
        score = overlap * 10
        if kind == "approval_history":
            score += 3
        if kind == "loop_summary":
            score += 2
        if created_at:
            score += 1
        ranked.append(((score, created_at, -idx), memory))

    ranked.sort(key=lambda item: item[0], reverse=True)
    return [memory for _, memory in ranked[:limit]]


def _planner_semantic_memory_prompt(repo_context: dict[str, Any], *, request: str) -> str:
    ranked = _rank_semantic_memories(request, repo_context, limit=4)
    if not ranked:
        return ""

    compact: list[dict[str, str]] = []
    for memory in ranked:
        compact.append(
            {
                "kind": str(memory.get("kind", "")).strip(),
                "source": str(memory.get("source", "")).strip(),
                "summary": str(memory.get("summary", "")).strip(),
                "created_at": str(memory.get("created_at", "")).strip(),
            }
        )
    return "semantic_memory_recall: " + json.dumps(compact, ensure_ascii=False, sort_keys=True)


def _rank_cached_procedures(request: str, repo_context: dict[str, Any], *, limit: int = 3) -> list[dict[str, Any]]:
    cached_raw = repo_context.get("cached_procedures", [])
    cached = (
        [dict(entry) for entry in cached_raw if isinstance(entry, dict)]
        if isinstance(cached_raw, list)
        else []
    )
    if not cached:
        return []

    request_tokens = set(_WORD_RE.findall(request.lower()))
    ranked: list[tuple[tuple[int, int, str, int], dict[str, Any]]] = []
    for idx, procedure in enumerate(cached):
        canonical_name = str(procedure.get("canonical_name", "")).strip()
        task_class = str(procedure.get("task_class", "")).strip()
        steps_raw = procedure.get("steps", [])
        steps = [dict(step) for step in steps_raw if isinstance(step, dict)] if isinstance(steps_raw, list) else []
        tool_names: list[str] = []
        for step in steps:
            tools_raw = step.get("tools", [])
            if not isinstance(tools_raw, list):
                continue
            for tool_call in tools_raw:
                if not isinstance(tool_call, dict):
                    continue
                tool_name = str(tool_call.get("tool", "")).strip()
                if tool_name and tool_name not in tool_names:
                    tool_names.append(tool_name)
        haystack = " ".join([canonical_name, task_class, *tool_names]).lower()
        overlap = len(request_tokens.intersection(set(_WORD_RE.findall(haystack))))
        use_count_raw = procedure.get("use_count", 0)
        use_count = int(use_count_raw) if isinstance(use_count_raw, int) else 0
        created_at = str(procedure.get("created_at", "")).strip()
        score = overlap * 10 + min(use_count, 5)
        ranked.append(((score, use_count, created_at, -idx), procedure))

    ranked.sort(key=lambda item: item[0], reverse=True)
    return [procedure for _, procedure in ranked[:limit]]


def _planner_procedural_memory_prompt(repo_context: dict[str, Any], *, request: str) -> str:
    ranked = _rank_cached_procedures(request, repo_context, limit=3)
    if not ranked:
        return ""

    compact: list[dict[str, Any]] = []
    for procedure in ranked:
        steps_raw = procedure.get("steps", [])
        step_count = len(steps_raw) if isinstance(steps_raw, list) else 0
        compact.append(
            {
                "procedure_id": str(procedure.get("procedure_id", "")).strip(),
                "canonical_name": str(procedure.get("canonical_name", "")).strip(),
                "task_class": str(procedure.get("task_class", "")).strip(),
                "use_count": int(procedure.get("use_count", 0) or 0),
                "step_count": step_count,
            }
        )
    return "procedural_memory_recall: " + json.dumps(compact, ensure_ascii=False, sort_keys=True)


def _semantic_memory_constraints(repo_context: dict[str, Any], *, request: str) -> dict[str, Any]:
    ranked = _rank_semantic_memories(request, repo_context, limit=4)
    if not ranked:
        return {"files_touched": [], "acceptance_criteria": [], "handoff_constraints": [], "handoff_evidence": []}

    acceptance_criteria: list[str] = []
    handoff_constraints: list[str] = []
    handoff_evidence: list[dict[str, str]] = []
    file_hints: list[str] = []

    for memory in ranked:
        kind = str(memory.get("kind", "")).strip()
        source = str(memory.get("source", "")).strip()
        summary = str(memory.get("summary", "")).strip()
        if not summary:
            continue

        if kind in {"approval_history", "approval_summary"}:
            acceptance_criteria.append(
                "Approval-sensitive changes preserve checkpoint-backed resume and auditability."
            )
            handoff_constraints.append(
                "Preserve approval and checkpoint compatibility for approval-sensitive mutations."
            )
        elif kind == "loop_summary":
            acceptance_criteria.append(
                "Cross-run lessons recalled from semantic memory are incorporated into the bounded plan."
            )
            handoff_constraints.append(
                "Do not repeat a previously failed repair pattern without a concrete change in approach."
            )
        else:
            acceptance_criteria.append(
                "Relevant recalled run knowledge is reflected in the plan."
            )

        handoff_evidence.append(
            {
                "kind": "semantic_memory",
                "ref": f"{kind}:{source}" if source else kind,
                "detail": summary,
            }
        )
        file_hints.extend(_FILE_HINT_RE.findall(summary))

    return {
        "files_touched": _dedupe_strings(file_hints)[:6],
        "acceptance_criteria": _dedupe_strings(acceptance_criteria),
        "handoff_constraints": _dedupe_strings(handoff_constraints),
        "handoff_evidence": handoff_evidence[:4],
    }


def _apply_semantic_memory_constraints(
    plan_payload: dict[str, Any],
    *,
    repo_context: dict[str, Any],
    request: str,
) -> dict[str, Any]:
    constraints = _semantic_memory_constraints(repo_context, request=request)
    if not any(constraints.values()):
        return plan_payload

    acceptance_raw = plan_payload.get("acceptance_criteria", [])
    acceptance = [entry for entry in acceptance_raw if isinstance(entry, str)] if isinstance(acceptance_raw, list) else []
    plan_payload["acceptance_criteria"] = _dedupe_strings(acceptance + list(constraints["acceptance_criteria"]))

    steps_raw = plan_payload.get("steps", [])
    if not isinstance(steps_raw, list):
        return plan_payload

    updated_steps: list[dict[str, Any]] = []
    for step in steps_raw:
        if not isinstance(step, dict):
            updated_steps.append(step)
            continue
        updated_step = dict(step)
        files_touched_raw = updated_step.get("files_touched", [])
        files_touched = [entry for entry in files_touched_raw if isinstance(entry, str)] if isinstance(files_touched_raw, list) else []
        updated_step["files_touched"] = _dedupe_strings(files_touched + list(constraints["files_touched"]))

        handoff_raw = updated_step.get("handoff")
        handoff = dict(handoff_raw) if isinstance(handoff_raw, dict) else None
        if handoff is not None and str(handoff.get("consumer", "")).strip() == "coder":
            file_scope_raw = handoff.get("file_scope", [])
            file_scope = [entry for entry in file_scope_raw if isinstance(entry, str)] if isinstance(file_scope_raw, list) else []
            handoff["file_scope"] = _dedupe_strings(file_scope + updated_step["files_touched"])

            existing_constraints_raw = handoff.get("constraints", [])
            existing_constraints = [entry for entry in existing_constraints_raw if isinstance(entry, str)] if isinstance(existing_constraints_raw, list) else []
            handoff["constraints"] = _dedupe_strings(existing_constraints + list(constraints["handoff_constraints"]))

            existing_evidence_raw = handoff.get("evidence", [])
            existing_evidence = [dict(entry) for entry in existing_evidence_raw if isinstance(entry, dict)] if isinstance(existing_evidence_raw, list) else []
            handoff["evidence"] = existing_evidence + [dict(entry) for entry in constraints["handoff_evidence"]]
            updated_step["handoff"] = handoff

        updated_steps.append(updated_step)

    plan_payload["steps"] = updated_steps
    return plan_payload


def _apply_procedural_memory_constraints(
    plan_payload: dict[str, Any],
    *,
    repo_context: dict[str, Any],
    request: str,
) -> tuple[dict[str, Any], str | None]:
    ranked = _rank_cached_procedures(request, repo_context, limit=1)
    if not ranked:
        return plan_payload, None

    procedure = ranked[0]
    procedure_id = str(procedure.get("procedure_id", "")).strip() or None
    canonical_name = str(procedure.get("canonical_name", "")).strip() or "cached_procedure"
    verification_raw = procedure.get("verification", [])
    verification = [dict(entry) for entry in verification_raw if isinstance(entry, dict)] if isinstance(verification_raw, list) else []

    if verification and not plan_payload.get("verification"):
        plan_payload["verification"] = verification

    acceptance_raw = plan_payload.get("acceptance_criteria", [])
    acceptance = [entry for entry in acceptance_raw if isinstance(entry, str)] if isinstance(acceptance_raw, list) else []
    acceptance.append(f"Validated procedure memory '{canonical_name}' is reused when compatible with current evidence.")
    plan_payload["acceptance_criteria"] = _dedupe_strings(acceptance)

    steps_raw = plan_payload.get("steps", [])
    if not isinstance(steps_raw, list):
        return plan_payload, procedure_id

    updated_steps: list[dict[str, Any]] = []
    for step in steps_raw:
        if not isinstance(step, dict):
            updated_steps.append(step)
            continue
        updated_step = dict(step)
        handoff_raw = updated_step.get("handoff")
        handoff = dict(handoff_raw) if isinstance(handoff_raw, dict) else None
        if handoff is not None and str(handoff.get("consumer", "")).strip() == "coder":
            constraints_raw = handoff.get("constraints", [])
            constraints = [entry for entry in constraints_raw if isinstance(entry, str)] if isinstance(constraints_raw, list) else []
            constraints.append(
                f"Prefer the validated cached procedure '{canonical_name}' when it remains compatible with current evidence."
            )
            handoff["constraints"] = _dedupe_strings(constraints)

            evidence_raw = handoff.get("evidence", [])
            evidence = [dict(entry) for entry in evidence_raw if isinstance(entry, dict)] if isinstance(evidence_raw, list) else []
            evidence.append(
                {
                    "kind": "procedure_cache",
                    "ref": procedure_id or canonical_name,
                    "detail": f"Validated procedure '{canonical_name}' is available for reuse.",
                }
            )
            handoff["evidence"] = evidence
            updated_step["handoff"] = handoff
        updated_steps.append(updated_step)

    plan_payload["steps"] = updated_steps
    return plan_payload, procedure_id


def _record_selected_procedure_use(state: dict[str, Any], *, procedure_id: str | None) -> None:
    if procedure_id is None:
        return
    procedure_cache_path = str(state.get("_procedure_cache_path", "")).strip()
    if not procedure_cache_path:
        return
    try:
        from lg_orch.procedure_cache import ProcedureCache

        cache = ProcedureCache(db_path=Path(procedure_cache_path))
        try:
            cache.record_use(procedure_id, used_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"))
        finally:
            cache.close()
    except Exception:
        return
