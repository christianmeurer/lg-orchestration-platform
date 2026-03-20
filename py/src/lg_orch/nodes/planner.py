"""Intent classification and main planner_node orchestrator."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema

from lg_orch.logging import get_logger
from lg_orch.memory import (
    ensure_history_policy,
    get_compression_summary,
    prune_pre_verification_history,
)
from lg_orch.model_routing import latest_model_route, record_inference_telemetry, record_model_route
from lg_orch.nodes._planner_memory import (
    _apply_procedural_memory_constraints,
    _apply_semantic_memory_constraints,
    _planner_procedural_memory_prompt,
    _planner_semantic_memory_prompt,
    _record_selected_procedure_use,
)
from lg_orch.nodes._planner_prompt import (
    _build_planner_prompts,
    _classify_intent,
    _default_plan,
    _extract_json_block,
    _first_step_handoff,
    _format_mcp_tool_catalog,  # re-exported: tests import it from this module
    _recovery_action_from_packet,
)
from lg_orch.state import PlannerOutput
from lg_orch.tools import InferenceClient
from lg_orch.trace import append_event

_SCHEMA_PATH = Path(__file__).parent.parent.parent.parent.parent / "schemas" / "planner_output.schema.json"


def _load_planner_schema() -> dict[str, Any]:
    try:
        return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))  # type: ignore[no-any-return]
    except Exception:
        return {}


PLANNER_SCHEMA: dict[str, Any] = _load_planner_schema()


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

    if provider == "openai_compatible":
        oc_raw = runtime.get("openai_compatible", {})
        oc_cfg = oc_raw if isinstance(oc_raw, dict) else {}
        api_key = str(oc_cfg.get("api_key", "")).strip()
        if not api_key:
            return None, None
        base_url = str(oc_cfg.get("base_url", "https://api.openai.com/v1")).strip().rstrip("/")
        if not base_url:
            return None, None
        if not (base_url.startswith("http://") or base_url.startswith("https://")):
            return None, None
        timeout_raw = oc_cfg.get("timeout_s", 60)
        timeout_s = int(timeout_raw) if isinstance(timeout_raw, int) and timeout_raw > 0 else 60
    else:
        do_raw = runtime.get("digitalocean", {})
        do_cfg = do_raw if isinstance(do_raw, dict) else {}
        api_key = str(do_cfg.get("api_key", "")).strip()
        if not api_key:
            return None, None
        base_url = str(do_cfg.get("base_url", "https://inference.do-ai.run/v1")).strip().rstrip("/")
        if not base_url:
            return None, None
        if not (base_url.startswith("http://") or base_url.startswith("https://")):
            return None, None
        timeout_raw = do_cfg.get("timeout_s", 60)
        timeout_s = int(timeout_raw) if isinstance(timeout_raw, int) and timeout_raw > 0 else 60

    repo_root = Path(str(state.get("_repo_root", "."))).resolve()
    repo_context_raw = state.get("repo_context", {})
    repo_context = repo_context_raw if isinstance(repo_context_raw, dict) else {}
    route_raw = state.get("route", {})
    route = dict(route_raw) if isinstance(route_raw, dict) else {}
    verification_raw = state.get("verification", {})
    verification = dict(verification_raw) if isinstance(verification_raw, dict) else {}
    system_prompt, user_prompt = _build_planner_prompts(
        state,
        repo_root=repo_root,
        repo_context=repo_context,
        route=route,
        verification=verification,
    )

    lane = str(route_decision.get("lane", "deep_planning")).strip()
    client = InferenceClient(base_url=base_url, api_key=api_key, timeout_s=timeout_s)
    try:
        if lane == "interactive":
            try:
                response = client.chat_completion_stream_sync(
                    model=model,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    temperature=max(0.0, min(temperature, 1.0)),
                    max_tokens=1400,
                )
            except Exception:
                response = client.chat_completion(
                    model=model,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    temperature=max(0.0, min(temperature, 1.0)),
                    max_tokens=1400,
                )
        else:
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
    if PLANNER_SCHEMA:
        try:
            jsonschema.validate(instance=parsed, schema=PLANNER_SCHEMA)
        except jsonschema.ValidationError as ve:
            log = get_logger()
            log.warning("planner_schema_validation_failed", error=str(ve.message))
            return None, None
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
    repo_context_raw = state.get("repo_context", {})
    repo_context = repo_context_raw if isinstance(repo_context_raw, dict) else {}
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
        recovery_packet_raw = state.get("recovery_packet", verification.get("recovery_packet", {}))
        recovery_packet = (
            dict(recovery_packet_raw) if isinstance(recovery_packet_raw, dict) else {}
        )
        if plan_payload.get("recovery") is None and isinstance(verification.get("recovery"), dict):
            plan_payload["recovery"] = dict(verification["recovery"])
        elif plan_payload.get("recovery") is None and recovery_packet:
            plan_payload["recovery"] = _recovery_action_from_packet(recovery_packet)
        if plan_payload.get("recovery_packet") is None and recovery_packet:
            plan_payload["recovery_packet"] = recovery_packet
        plan_payload = _apply_semantic_memory_constraints(
            plan_payload,
            repo_context=repo_context,
            request=request,
        )
        plan_payload, procedure_id = _apply_procedural_memory_constraints(
            plan_payload,
            repo_context=repo_context,
            request=request,
        )
        _record_selected_procedure_use(state, procedure_id=procedure_id)
        out = {
            **state,
            "intent": intent,
            "plan": plan_payload,
            "active_handoff": _first_step_handoff(plan_payload),
        }
        out = record_inference_telemetry(
            out,
            node_name="planner",
            provider=str(route_decision.get("provider", "")),
            model=str(route_decision.get("model", "")),
            response=response,
        )
        telemetry_raw = out.get("telemetry", {})
        telemetry = dict(telemetry_raw) if isinstance(telemetry_raw, dict) else {}
        telemetry["compression_summary"] = get_compression_summary(out)
        out = {**out, "telemetry": telemetry}
        step_count = len(out.get("plan", {}).get("steps", []))
        out = append_event(
            out,
            kind="node",
            data={"name": "planner", "phase": "end", "steps": step_count},
        )
        return prune_pre_verification_history(out)
    except Exception as exc:
        log.error("planner_failed", error=str(exc))
        fallback_plan = _default_plan(request).model_dump()
        fallback_plan["rollback"] = "Plan generation failed; deterministic fallback used."
        fallback_plan = _apply_semantic_memory_constraints(
            fallback_plan,
            repo_context=repo_context,
            request=request,
        )
        fallback_plan, procedure_id = _apply_procedural_memory_constraints(
            fallback_plan,
            repo_context=repo_context,
            request=request,
        )
        _record_selected_procedure_use(state, procedure_id=procedure_id)
        out = {
            **state,
            "intent": str(route.get("intent", "analysis") or "analysis"),
            "plan": fallback_plan,
            "active_handoff": _first_step_handoff(fallback_plan),
        }
        step_count = len(out.get("plan", {}).get("steps", []))
        out = append_event(
            out,
            kind="node",
            data={"name": "planner", "phase": "end", "steps": step_count},
        )
        return prune_pre_verification_history(out)


# Re-export alias kept for backward compatibility
planner_node = planner
