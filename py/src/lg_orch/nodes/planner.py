from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from lg_orch.logging import get_logger
from lg_orch.memory import ensure_history_policy, prune_pre_verification_history
from lg_orch.model_routing import latest_model_route, record_inference_telemetry, record_model_route
from lg_orch.state import PlannerOutput, PlanStep, ToolCall
from lg_orch.tools import InferenceClient
from lg_orch.trace import append_event

_WORD_RE = re.compile(r"[a-z0-9']+")
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

    return "\n".join(parts)


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

    return PlannerOutput(
        steps=[
            PlanStep(
                id="step-1",
                description="Collect repository context.",
                tools=tools,
                expected_outcome=expected_outcome,
                files_touched=[],
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


def _planner_model_output(
    state: dict[str, Any],
    *,
    route_decision: dict[str, Any],
) -> tuple[PlannerOutput | None, Any | None]:
    if str(route_decision.get("provider_used", "local")).strip() == "local":
        return None, None

    models_raw = state.get("_models", {})
    models = models_raw if isinstance(models_raw, dict) else {}
    slot_raw = models.get("planner", {})
    slot = slot_raw if isinstance(slot_raw, dict) else {}
    provider = str(slot.get("provider", "local")).strip().lower()
    if provider in {"", "local"}:
        return None, None

    model = str(slot.get("model", "deterministic")).strip()
    if not model:
        return None, None
    temperature_raw = slot.get("temperature", 0.0)
    temperature = float(temperature_raw) if isinstance(temperature_raw, (int, float)) else 0.0

    runtime_raw = state.get("_model_provider_runtime", {})
    runtime = runtime_raw if isinstance(runtime_raw, dict) else {}
    do_raw = runtime.get("digitalocean", {})
    do_cfg = do_raw if isinstance(do_raw, dict) else {}
    api_key = str(do_cfg.get("api_key", "")).strip()
    if not api_key:
        return None, None
    base_url = str(do_cfg.get("base_url", "https://inference.do-ai.run/v1")).strip().rstrip("/")
    if not base_url:
        return None, None
    timeout_raw = do_cfg.get("timeout_s", 60)
    timeout_s = int(timeout_raw) if isinstance(timeout_raw, int) and timeout_raw > 0 else 60

    repo_root = Path(str(state.get("_repo_root", "."))).resolve()
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

    schema_text = ""
    try:
        if schema_path.is_file():
            schema_text = schema_path.read_text(encoding="utf-8")
    except OSError:
        schema_text = ""

    request = str(state.get("request", "")).strip()
    repo_context_raw = state.get("repo_context", {})
    repo_context = repo_context_raw if isinstance(repo_context_raw, dict) else {}
    top_level = repo_context.get("top_level", [])
    top_level_s = ", ".join([str(x) for x in top_level[:30]]) if isinstance(top_level, list) else ""
    planner_context_raw = repo_context.get("planner_context", {})
    planner_context = dict(planner_context_raw) if isinstance(planner_context_raw, dict) else {}
    route_raw = state.get("route", {})
    route = dict(route_raw) if isinstance(route_raw, dict) else {}
    verification_raw = state.get("verification", {})
    verification = dict(verification_raw) if isinstance(verification_raw, dict) else {}
    budgets = {
        "max_tool_calls_per_loop": int(state.get("_budget_max_tool_calls_per_loop", 0) or 0),
        "max_patch_bytes": int(state.get("_budget_max_patch_bytes", 0) or 0),
        "max_loops": int(state.get("_budget_max_loops", 1) or 1),
    }
    mcp_prompt = _planner_mcp_prompt(repo_context)
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
    if schema_text:
        user_prompt = f"{user_prompt}\nschema:\n{schema_text}"

    client = InferenceClient(base_url=base_url, api_key=api_key, timeout_s=timeout_s)
    try:
        response = client.chat_completion(
            model=model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=max(0.0, min(temperature, 1.0)),
            max_tokens=1400,
        )
    finally:
        client.close()

    raw = response if isinstance(response, str) else response.text
    parsed = json.loads(_extract_json_block(raw))
    if not isinstance(parsed, dict):
        raise ValueError("planner completion did not return an object")
    return PlannerOutput.model_validate(parsed), response


def planner(state: dict[str, Any]) -> dict[str, Any]:
    log = get_logger()
    state = ensure_history_policy(state)
    state = record_model_route(
        state,
        node_name="planner",
        task_class=str(state.get("route", {}).get("task_class", "context_condensation")),
        model_slot="planner",
    )
    state = append_event(state, kind="node", data={"name": "planner", "phase": "start"})

    if bool(state.get("context_reset_requested", False)):
        state = {
            **state,
            "plan": None,
            "facts": [],
            "context_reset_requested": False,
            "plan_discarded": False,
            "plan_discard_reason": "",
            "retry_target": None,
        }

    request = str(state.get("request", "")).strip()
    route_decision = latest_model_route(state, node_name="planner")
    route_raw = state.get("route", {})
    route = dict(route_raw) if isinstance(route_raw, dict) else {}
    try:
        intent = str(route.get("intent", "")).strip() or _classify_intent(request)
        remote_plan, response = _planner_model_output(state, route_decision=route_decision)
        plan = remote_plan if remote_plan is not None else _default_plan(request)
        plan_payload = plan.model_dump()
        configured_max_loops = int(state.get("_budget_max_loops", 1) or 1)
        plan_payload["max_iterations"] = max(
            1,
            min(int(plan_payload.get("max_iterations", 1) or 1), configured_max_loops),
        )
        if not plan_payload.get("acceptance_criteria"):
            plan_payload["acceptance_criteria"] = _default_plan(request).acceptance_criteria
        verification_raw = state.get("verification", {})
        verification = dict(verification_raw) if isinstance(verification_raw, dict) else {}
        if plan_payload.get("recovery") is None and isinstance(verification.get("recovery"), dict):
            plan_payload["recovery"] = dict(verification["recovery"])
        out = {**state, "intent": intent, "plan": plan_payload}
        out = record_inference_telemetry(
            out,
            node_name="planner",
            provider=str(route_decision.get("provider", "")),
            model=str(route_decision.get("model", "")),
            response=response,
        )
    except Exception as exc:
        log.error("planner_failed", error=str(exc))
        fallback_plan = _default_plan(request).model_dump()
        fallback_plan["rollback"] = "Plan generation failed; deterministic fallback used."
        out = {**state, "intent": str(route.get("intent", "analysis") or "analysis"), "plan": fallback_plan}
    step_count = len(out.get("plan", {}).get("steps", []))
    out = append_event(
        out,
        kind="node",
        data={"name": "planner", "phase": "end", "steps": step_count},
    )
    return prune_pre_verification_history(out)
