"""Prompt construction, schema validation, and deterministic fallback plan for the planner."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from lg_orch.nodes._planner_memory import _WORD_RE
from lg_orch.state import AgentHandoff, HandoffEvidence, PlannerOutput, PlanStep, ToolCall

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.DOTALL | re.IGNORECASE)
_PDF_PATH_RE = re.compile(r'(["\']?)([^"\'\n\r]*?\.pdf)\1', re.IGNORECASE)


def _classify_intent(request: str) -> str:
    r = request.lower()
    words = set(_WORD_RE.findall(r))
    if ("fix" in words) or ("fix" in r):
        return "code_change"
    if words.intersection({"implement", "add", "change", "refactor"}):
        return "code_change"
    if (
        "why" in words
        or "how" in words
        or "explain" in words
        or re.search(r"\bwhat\s+is\b", r) is not None
    ):
        return "question"
    if words.intersection({"research", "latest", "compare", "survey"}):
        return "research"
    if words.intersection({"debug", "error", "panic", "exception"}) or "stack trace" in r:
        return "debug"
    return "analysis"


def _extract_json_block(raw: str) -> str:
    fenced = _JSON_FENCE_RE.search(raw)
    if fenced is not None:
        return fenced.group(1).strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1 and end >= start:
        return raw[start : end + 1].strip()
    return raw.strip()


def _extract_pdf_path(request: str) -> str | None:
    match = _PDF_PATH_RE.search(request)
    if match is None:
        return None
    candidate = match.group(2).strip()
    if not candidate:
        return None
    return candidate


def _planner_mcp_prompt(repo_context: dict[str, Any]) -> str:
    parts: list[str] = []

    mcp_catalog = str(repo_context.get("mcp_catalog", "")).strip()
    if mcp_catalog:
        parts.append(f"mcp_catalog: {mcp_catalog}")

    mcp_capabilities_raw = repo_context.get("mcp_capabilities", {})
    if isinstance(mcp_capabilities_raw, dict) and mcp_capabilities_raw:
        parts.append(
            "mcp_capabilities: " + json.dumps(mcp_capabilities_raw, ensure_ascii=False, sort_keys=True)
        )

    mcp_recovery_hints = str(repo_context.get("mcp_recovery_hints", "")).strip()
    if mcp_recovery_hints:
        parts.append(f"mcp_recovery_hints: {mcp_recovery_hints}")

    mcp_relevant_tools_raw = repo_context.get("mcp_relevant_tools", [])
    if isinstance(mcp_relevant_tools_raw, list) and mcp_relevant_tools_raw:
        parts.append(
            "mcp_relevant_tools: "
            + json.dumps(mcp_relevant_tools_raw, ensure_ascii=False, sort_keys=True)
        )

    return "\n".join(parts)


def _format_mcp_tool_catalog(mcp_tools: list[dict[str, Any]]) -> str:
    """Format a runtime-discovered MCP tool list as a ## Available MCP Tools block.

    Returns an empty string when *mcp_tools* is empty so callers can guard with
    ``if catalog:`` without producing empty section headers.
    """
    lines: list[str] = []
    for tool in mcp_tools:
        name = str(tool.get("name", "")).strip()
        description = str(tool.get("description", "")).strip()
        if not name:
            continue
        lines.append(f"- `{name}`: {description}" if description else f"- `{name}`")
        input_schema = tool.get("inputSchema") or tool.get("input_schema")
        if isinstance(input_schema, dict):
            props = input_schema.get("properties", {})
            if isinstance(props, dict) and props:
                schema_summary = {
                    k: str(v.get("type", "any"))
                    for k, v in props.items()
                    if isinstance(v, dict)
                }
                lines.append(f"  Input schema: {schema_summary}")
    if not lines:
        return ""
    return "## Available MCP Tools\n" + "\n".join(lines)


def _default_step_handoff(request: str, *, step_id: str, expected_outcome: str) -> AgentHandoff | None:
    intent = _classify_intent(request)
    if intent not in {"code_change", "refactor", "debug"}:
        return None

    objective = "Prepare a minimal patch proposal grounded in the gathered repository context."
    constraints = [
        "Prefer the smallest correct diff.",
        "Stay within the declared file scope or hand back a narrower follow-up request.",
        "Keep the change compatible with the planned verification steps.",
    ]
    if intent == "debug":
        objective = "Prepare a minimal repair grounded in the gathered repository context and failing evidence."
        constraints.append("Preserve the failing reproduction until the fix is ready for verification.")

    return AgentHandoff(
        producer="planner",
        consumer="coder",
        objective=objective,
        file_scope=[],
        evidence=[
            HandoffEvidence(kind="request", ref="user_request", detail=request.strip()),
            HandoffEvidence(kind="expected_outcome", ref=step_id, detail=expected_outcome),
        ],
        constraints=constraints,
        acceptance_checks=[
            "The proposed patch is grounded in gathered repository context.",
            "The change remains minimal and reviewable.",
        ],
        retry_budget=1,
        provenance=[f"plan:{step_id}"],
    )


def _first_step_handoff(plan_payload: dict[str, Any]) -> dict[str, Any] | None:
    steps_raw = plan_payload.get("steps", [])
    if not isinstance(steps_raw, list):
        return None

    for step in steps_raw:
        if not isinstance(step, dict):
            continue
        handoff_raw = step.get("handoff")
        if isinstance(handoff_raw, dict):
            return dict(handoff_raw)
    return None


def _default_plan(request: str = "") -> PlannerOutput:
    tools: list[ToolCall] = [ToolCall(tool="list_files", input={"path": ".", "recursive": False})]
    expected_outcome = "Top-level repository structure captured."

    pdf_path = _extract_pdf_path(request)
    if pdf_path is not None:
        tools.append(ToolCall(tool="read_file", input={"path": pdf_path}))
        expected_outcome = "Top-level repository structure and PDF requirements extracted."
    else:
        tools.append(
            ToolCall(
                tool="search_files",
                input={"path": ".", "regex": "TODO", "file_pattern": "*.py"},
            )
        )
        expected_outcome = "Top-level repository structure and TODOs captured."

    step_id = "step-1"

    return PlannerOutput(
        steps=[
            PlanStep(
                id=step_id,
                description="Collect repository context.",
                tools=tools,
                expected_outcome=expected_outcome,
                files_touched=[],
                handoff=_default_step_handoff(
                    request,
                    step_id=step_id,
                    expected_outcome=expected_outcome,
                ),
            )
        ],
        verification=[],
        rollback="No changes were made.",
        acceptance_criteria=[
            "Necessary repository context was gathered.",
            "The request can be answered or executed with bounded next steps.",
        ],
        max_iterations=1,
    )


def _recovery_action_from_packet(packet: dict[str, Any]) -> dict[str, Any]:
    return {
        "failure_class": str(packet.get("failure_class", "")).strip(),
        "failure_fingerprint": str(packet.get("failure_fingerprint", "")).strip(),
        "rationale": str(packet.get("rationale", "")).strip(),
        "retry_target": str(packet.get("retry_target", "planner")).strip() or "planner",
        "context_scope": str(packet.get("context_scope", "working_set")).strip() or "working_set",
        "plan_action": str(packet.get("plan_action", "keep")).strip() or "keep",
    }


def _build_planner_prompts(
    state: dict[str, Any],
    *,
    repo_root: Path,
    repo_context: dict[str, Any],
    route: dict[str, Any],
    verification: dict[str, Any],
) -> tuple[str, str]:
    """Build (system_prompt, user_prompt) for the planner LLM call."""
    from lg_orch.nodes._planner_memory import (
        _planner_procedural_memory_prompt,
        _planner_semantic_memory_prompt,
    )

    planner_prompt_path = repo_root / "prompts" / "planner.md"
    schema_path = repo_root / "schemas" / "planner_output.schema.json"

    system_prompt = "You are a planner for a repo-aware coding assistant. Return strict JSON only."
    try:
        if planner_prompt_path.is_file():
            prompt_text = planner_prompt_path.read_text(encoding="utf-8").strip()
            if prompt_text:
                system_prompt = prompt_text
    except OSError:
        pass

    if bool(state.get("test_repair_mode", False)):
        repair_prefix = (
            "REPAIR MODE: The task is to fix one or more failing tests. Focus exclusively on:\n"
            "1. Reading the failing test(s) and the code they test.\n"
            "2. Identifying the root cause (test expectation vs. implementation mismatch).\n"
            "3. Generating a targeted patch to fix the implementation (not the test, unless the test itself is incorrect).\n"
            "4. Verifying the fix by re-running only the affected test(s).\n"
            "Do not refactor unrelated code.\n\n"
        )
        system_prompt = repair_prefix + system_prompt

    schema_text = ""
    try:
        if schema_path.is_file():
            schema_text = schema_path.read_text(encoding="utf-8")
    except OSError:
        pass

    request = str(state.get("request", "")).strip()
    top_level = repo_context.get("top_level", [])
    top_level_s = ", ".join([str(x) for x in top_level[:30]]) if isinstance(top_level, list) else ""
    planner_context_raw = repo_context.get("planner_context", {})
    planner_context = dict(planner_context_raw) if isinstance(planner_context_raw, dict) else {}
    budgets = {
        "max_tool_calls_per_loop": int(state.get("_budget_max_tool_calls_per_loop", 0) or 0),
        "max_patch_bytes": int(state.get("_budget_max_patch_bytes", 0) or 0),
        "max_loops": int(state.get("_budget_max_loops", 1) or 1),
    }
    mcp_prompt = _planner_mcp_prompt(repo_context)
    mcp_tools_raw = state.get("mcp_tools", [])
    mcp_tools: list[dict[str, Any]] = (
        [t for t in mcp_tools_raw if isinstance(t, dict)]
        if isinstance(mcp_tools_raw, list)
        else []
    )
    mcp_tool_catalog = _format_mcp_tool_catalog(mcp_tools)
    semantic_memory_prompt = _planner_semantic_memory_prompt(repo_context, request=request)
    procedural_memory_prompt = _planner_procedural_memory_prompt(repo_context, request=request)

    user_prompt = (
        "Create a bounded execution plan for the request below."
        " The response must be JSON matching planner_output.schema.json."
        " Do not include prose outside JSON.\n\n"
        f"request: {request}\n"
        f"top_level: {top_level_s}\n"
        f"route: {json.dumps(route, ensure_ascii=False)}\n"
        f"planner_context: {planner_context.get('content', '')}\n"
        f"verification: {json.dumps(verification, ensure_ascii=False)}\n"
        f"budgets: {json.dumps(budgets, ensure_ascii=False)}\n"
    )
    if mcp_prompt:
        user_prompt = f"{user_prompt}{mcp_prompt}\n"
    if mcp_tool_catalog:
        user_prompt = f"{user_prompt}{mcp_tool_catalog}\n"
    if semantic_memory_prompt:
        user_prompt = f"{user_prompt}{semantic_memory_prompt}\n"
    if procedural_memory_prompt:
        user_prompt = f"{user_prompt}{procedural_memory_prompt}\n"
    if schema_text:
        user_prompt = f"{user_prompt}\nschema:\n{schema_text}"

    return system_prompt, user_prompt
