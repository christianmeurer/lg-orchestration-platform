# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Christian Meurer — https://github.com/christianmeurer/Lula
from __future__ import annotations

import json
from hashlib import sha256
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

if TYPE_CHECKING:
    from lg_orch.long_term_memory import LongTermMemoryStore

_PRUNED_READ_FILE_PREFIX = "[pruned_read_file_payload]"

_HISTORY_POLICY_DEFAULTS: dict[str, int] = {
    "schema_version": 1,
    "retain_recent_tool_results": 40,
    "read_file_prune_threshold_chars": 4_000,
}

_CONTEXT_BUDGET_DEFAULTS: dict[str, int] = {
    "stable_prefix_tokens": 1600,
    "working_set_tokens": 1600,
    "tool_result_summary_chars": 480,
}

_COMPRESSION_PROVENANCE_VERSION = 1

HistoryPolicy = dict[str, int]


def _state_get(state: object, key: str, default: object = None) -> object:
    """Safely access a field from either a Pydantic BaseModel or a plain dict."""
    if isinstance(state, BaseModel):
        if hasattr(state, "model_extra") and state.model_extra and key in state.model_extra:
            return state.model_extra[key]
        return getattr(state, key, default)
    if isinstance(state, dict):
        return state.get(key, default)
    return default


def _state_to_dict(state: object) -> dict[str, Any]:
    """Convert a Pydantic model or dict to a plain dict."""
    if isinstance(state, BaseModel):
        d: dict[str, Any] = {}
        for field in state.model_fields:
            d[field] = getattr(state, field, None)
        if hasattr(state, "model_extra") and state.model_extra:
            d.update(state.model_extra)
        return d
    if isinstance(state, dict):
        return dict(state)
    return {}


def _as_int(value: object, *, default: int, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        return default

    parsed: int
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, float):
        parsed = int(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return default
        try:
            parsed = int(text)
        except ValueError:
            return default
    else:
        return default

    return min(max(parsed, minimum), maximum)


def _normalize_history_policy(raw: dict[str, Any]) -> HistoryPolicy:
    return {
        "schema_version": _HISTORY_POLICY_DEFAULTS["schema_version"],
        "retain_recent_tool_results": _as_int(
            raw.get("retain_recent_tool_results"),
            default=_HISTORY_POLICY_DEFAULTS["retain_recent_tool_results"],
            minimum=5,
            maximum=500,
        ),
        "read_file_prune_threshold_chars": _as_int(
            raw.get("read_file_prune_threshold_chars"),
            default=_HISTORY_POLICY_DEFAULTS["read_file_prune_threshold_chars"],
            minimum=200,
            maximum=200_000,
        ),
    }


def _tool_results(state: object) -> list[dict[str, Any]]:
    raw = _state_get(state, "tool_results", [])
    if not isinstance(raw, list):
        return []
    return [entry for entry in raw if isinstance(entry, dict)]


def _provenance(state: object) -> list[dict[str, Any]]:
    raw = _state_get(state, "provenance", [])
    if not isinstance(raw, list):
        return []
    return [entry for entry in raw if isinstance(entry, dict)]


def approx_token_count(text: str) -> int:
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def context_budget_settings(state: object) -> dict[str, int]:
    raw = _state_get(state, "_budget_context", {})
    src = raw if isinstance(raw, dict) else {}
    return {
        "stable_prefix_tokens": _as_int(
            src.get("stable_prefix_tokens"),
            default=_CONTEXT_BUDGET_DEFAULTS["stable_prefix_tokens"],
            minimum=200,
            maximum=100_000,
        ),
        "working_set_tokens": _as_int(
            src.get("working_set_tokens"),
            default=_CONTEXT_BUDGET_DEFAULTS["working_set_tokens"],
            minimum=200,
            maximum=100_000,
        ),
        "tool_result_summary_chars": _as_int(
            src.get("tool_result_summary_chars"),
            default=_CONTEXT_BUDGET_DEFAULTS["tool_result_summary_chars"],
            minimum=80,
            maximum=20_000,
        ),
    }


def _safe_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(value)


def _first_nonempty_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def dedupe_semantic_hits(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    best_by_key: dict[str, dict[str, Any]] = {}
    for hit in hits:
        if not isinstance(hit, dict):
            continue  # type: ignore[unreachable]
        path = str(hit.get("path", "")).strip()
        snippet = _first_nonempty_line(str(hit.get("snippet", "")))
        key = path or snippet
        if not key:
            continue
        current = best_by_key.get(key)
        score_raw = hit.get("score", 0)
        score = float(score_raw) if isinstance(score_raw, (int, float)) else 0.0
        if current is None:
            best_by_key[key] = dict(hit)
            continue
        current_score_raw = current.get("score", 0)
        current_score = (
            float(current_score_raw) if isinstance(current_score_raw, (int, float)) else 0.0
        )
        if score > current_score:
            best_by_key[key] = dict(hit)
    deduped = list(best_by_key.values())
    deduped.sort(
        key=lambda item: (
            float(item.get("score", 0)) if isinstance(item.get("score"), (int, float)) else 0.0
        ),
        reverse=True,
    )
    return deduped


def summarize_tool_result(result: dict[str, Any], *, max_chars: int) -> dict[str, Any]:
    diagnostics_raw = result.get("diagnostics", [])
    diagnostics = diagnostics_raw if isinstance(diagnostics_raw, list) else []
    diagnostic_message = ""
    if diagnostics and isinstance(diagnostics[0], dict):
        diagnostic_message = str(diagnostics[0].get("message", "")).strip()

    detail = diagnostic_message or _first_nonempty_line(str(result.get("stderr", "")))
    if not detail:
        detail = _first_nonempty_line(str(result.get("stdout", "")))
    if len(detail) > max_chars:
        detail = detail[: max_chars - 3].rstrip() + "..."

    artifacts_raw = result.get("artifacts", {})
    artifacts = dict(artifacts_raw) if isinstance(artifacts_raw, dict) else {}
    return {
        "tool": str(result.get("tool", "")).strip(),
        "ok": bool(result.get("ok", False)),
        "exit_code": (
            int(result.get("exit_code", 0)) if isinstance(result.get("exit_code"), int) else 0
        ),
        "summary": detail,
        "error": str(artifacts.get("error", "")).strip(),
    }


def _truncate_text_to_budget(text: str, *, budget_tokens: int) -> tuple[str, bool]:
    if budget_tokens <= 0:
        return "", True
    max_chars = budget_tokens * 4
    if len(text) <= max_chars:
        return text, False
    if max_chars <= 64:
        return text[:max_chars].rstrip(), True
    marker = "\n...[compressed]...\n"
    head_chars = max((max_chars - len(marker)) * 2 // 3, 40)
    tail_chars = max(max_chars - len(marker) - head_chars, 20)
    return f"{text[:head_chars].rstrip()}{marker}{text[-tail_chars:].lstrip()}", True


def _fit_segments(
    segments: list[tuple[str, str]],
    *,
    budget_tokens: int,
) -> tuple[str, list[dict[str, Any]]]:
    remaining = max(budget_tokens, 0)
    chunks: list[str] = []
    decisions: list[dict[str, Any]] = []

    for label, text in segments:
        if not text.strip():
            continue
        block = f"[{label}]\n{text}".strip()
        tokens_before = approx_token_count(block)
        if remaining <= 0:
            decisions.append(
                {
                    "segment": label,
                    "action": "dropped",
                    "tokens_before": tokens_before,
                    "tokens_after": 0,
                }
            )
            continue
        if tokens_before <= remaining:
            chunks.append(block)
            remaining -= tokens_before
            continue

        compressed, did_compress = _truncate_text_to_budget(block, budget_tokens=remaining)
        tokens_after = approx_token_count(compressed)
        if compressed.strip():
            chunks.append(compressed)
        decisions.append(
            {
                "segment": label,
                "action": "compressed" if did_compress else "kept",
                "tokens_before": tokens_before,
                "tokens_after": tokens_after,
            }
        )
        remaining = max(remaining - tokens_after, 0)

    return "\n\n".join(chunk for chunk in chunks if chunk.strip()).strip(), decisions


def _compression_pressure(decisions: list[dict[str, Any]]) -> dict[str, Any]:
    compressed = 0
    dropped = 0
    kept = 0
    for decision in decisions:
        if not isinstance(decision, dict):
            continue  # type: ignore[unreachable]
        action = str(decision.get("action", "")).strip()
        if action == "compressed":
            compressed += 1
        elif action == "dropped":
            dropped += 1
        else:
            kept += 1
    return {
        "compressed_segments": compressed,
        "dropped_segments": dropped,
        "kept_segments": kept,
        "score": compressed + (dropped * 2),
    }


def _fact_pack(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for fact in facts:
        if not isinstance(fact, dict):
            continue  # type: ignore[unreachable]
        entry = dict(fact)
        fingerprint = str(entry.get("failure_fingerprint", "")).strip()
        summary = str(entry.get("summary", entry.get("loop_summary", ""))).strip()
        if not fingerprint and not summary:
            continue
        loop_raw = entry.get("loop", 0)
        salience_raw = entry.get("salience", 0)
        entry["kind"] = str(entry.get("kind", "fact")).strip() or "fact"
        entry["summary"] = summary
        entry["loop"] = (
            loop_raw if isinstance(loop_raw, int) and not isinstance(loop_raw, bool) else 0
        )
        entry["salience"] = (
            salience_raw
            if isinstance(salience_raw, int) and not isinstance(salience_raw, bool)
            else 0
        )
        normalized.append(entry)

    deduped: dict[str, dict[str, Any]] = {}
    for entry in normalized:
        key = (
            str(entry.get("failure_fingerprint", "")).strip()
            or str(entry.get("summary", "")).strip()
        )
        current = deduped.get(key)
        if current is None:
            deduped[key] = entry
            continue
        current_rank = (int(current.get("salience", 0) or 0), int(current.get("loop", 0) or 0))
        next_rank = (int(entry.get("salience", 0) or 0), int(entry.get("loop", 0) or 0))
        if next_rank >= current_rank:
            deduped[key] = entry

    fact_pack = list(deduped.values())
    fact_pack.sort(
        key=lambda item: (
            int(item.get("salience", 0) or 0),
            int(item.get("loop", 0) or 0),
            1 if str(item.get("kind", "")).strip() == "recovery_fact" else 0,
        ),
        reverse=True,
    )
    return fact_pack[:8]


def build_context_layers(
    *,
    state: dict[str, Any],
    repo_context: dict[str, Any],
    long_term: LongTermMemoryStore | None = None,
) -> dict[str, Any]:
    budgets = context_budget_settings(state)

    top_level_raw = repo_context.get("top_level", [])
    top_level = top_level_raw if isinstance(top_level_raw, list) else []
    semantic_hits_raw = repo_context.get("semantic_hits", [])
    semantic_hits = (
        dedupe_semantic_hits([hit for hit in semantic_hits_raw if isinstance(hit, dict)])
        if isinstance(semantic_hits_raw, list)
        else []
    )

    # --- long-term memory injection ---
    if long_term is not None:
        task_text = str(
            _state_get(state, "task", _state_get(state, "request", ""))
        ).strip()
        if task_text:
            lt_content = long_term.retrieve_for_context(task_text, max_tokens=1000)
            if lt_content.strip():
                # Prepend to stable_segments before any other segment
                stable_segments_pre: list[tuple[str, str]] = [("long_term_memories", lt_content)]
            else:
                stable_segments_pre = []
        else:
            stable_segments_pre = []
    else:
        stable_segments_pre = []

    stable_segments: list[tuple[str, str]] = [
        *stable_segments_pre,
        (
            "repo_summary",
            "\n".join(
                [
                    f"repo_root: {repo_context.get('repo_root', '')}",
                    f"has_py: {bool(repo_context.get('has_py', False))}",
                    f"has_rs: {bool(repo_context.get('has_rs', False))}",
                    f"top_level: {', '.join(str(item) for item in top_level[:30])}",
                ]
            ),
        ),
    ]

    # Store episodes for any finalized loop summaries when long_term is provided
    if long_term is not None:
        run_id_raw = _state_get(state, "run_id", "")
        run_id = str(run_id_raw).strip() if run_id_raw else ""
        if run_id:
            loop_summaries_raw = _state_get(state, "loop_summaries", [])
            loop_summaries_list = loop_summaries_raw if isinstance(loop_summaries_raw, list) else []
            for entry in loop_summaries_list:
                if not isinstance(entry, dict):
                    continue
                loop_summary_text = str(entry.get("loop_summary", entry.get("summary", ""))).strip()
                outcome_text = str(entry.get("outcome", entry.get("status", ""))).strip()
                if loop_summary_text:
                    long_term.store_episode(
                        run_id,
                        loop_summary_text,
                        outcome_text,
                        metadata={
                            "loop": entry.get("loop", 0),
                            "failure_class": entry.get("failure_class", ""),
                        },
                    )

    repo_map = str(repo_context.get("repo_map", "")).strip()
    if repo_map:
        stable_segments.append(("repo_map", repo_map))

    ast_map = repo_context.get("structural_ast_map")
    if ast_map:
        stable_segments.append(("structural_ast_map", _safe_json(ast_map)))

    if semantic_hits:
        stable_segments.append(("semantic_hits", _safe_json(semantic_hits[:8])))

    mcp_catalog = str(repo_context.get("mcp_catalog", "")).strip()
    if mcp_catalog:
        stable_segments.append(("mcp_catalog", mcp_catalog))

    episodic_facts_raw = repo_context.get("episodic_facts", [])
    if isinstance(episodic_facts_raw, list) and episodic_facts_raw:
        stable_segments.append(("episodic_facts", _safe_json(episodic_facts_raw[:5])))

    semantic_memories_raw = repo_context.get("semantic_memories", [])
    semantic_memories = (
        [entry for entry in semantic_memories_raw if isinstance(entry, dict)]
        if isinstance(semantic_memories_raw, list)
        else []
    )
    if semantic_memories:
        stable_segments.append(("semantic_memories", _safe_json(semantic_memories[:5])))

    mcp_recovery_hints = str(repo_context.get("mcp_recovery_hints", "")).strip()
    if mcp_recovery_hints:
        stable_segments.append(("mcp_recovery_hints", mcp_recovery_hints))

    verification_raw = _state_get(state, "verification", {})
    verification = dict(verification_raw) if isinstance(verification_raw, dict) else {}
    recovery_packet_raw = _state_get(state, "recovery_packet", verification.get("recovery_packet", {}))
    recovery_packet = dict(recovery_packet_raw) if isinstance(recovery_packet_raw, dict) else {}
    plan_raw = _state_get(state, "plan", {})
    plan = dict(plan_raw) if isinstance(plan_raw, dict) else {}
    facts_raw = _state_get(state, "facts", [])
    facts = facts_raw if isinstance(facts_raw, list) else []
    fact_pack = _fact_pack([fact for fact in facts if isinstance(fact, dict)])
    loop_summaries_raw = _state_get(state, "loop_summaries", [])
    loop_summaries = loop_summaries_raw if isinstance(loop_summaries_raw, list) else []

    recent_tool_summaries = [
        summarize_tool_result(result, max_chars=budgets["tool_result_summary_chars"])
        for result in _tool_results(state)[-6:]
    ]

    working_segments: list[tuple[str, str]] = []
    if verification:
        working_segments.append(
            (
                "verification",
                _safe_json(
                    {
                        "ok": verification.get("ok"),
                        "failure_class": verification.get("failure_class", ""),
                        "failure_fingerprint": verification.get("failure_fingerprint", ""),
                        "loop_summary": verification.get("loop_summary", ""),
                        "recovery": verification.get("recovery"),
                        "recovery_packet": verification.get("recovery_packet"),
                    }
                ),
            )
        )
    if recovery_packet:
        working_segments.append(
            (
                "recovery_packet",
                _safe_json(
                    {
                        "failure_class": recovery_packet.get("failure_class", ""),
                        "failure_fingerprint": recovery_packet.get("failure_fingerprint", ""),
                        "summary": recovery_packet.get("summary", ""),
                        "last_check": recovery_packet.get("last_check", ""),
                        "context_scope": recovery_packet.get("context_scope", ""),
                        "plan_action": recovery_packet.get("plan_action", ""),
                        "retry_target": recovery_packet.get("retry_target", ""),
                    }
                ),
            )
        )
    if fact_pack:
        working_segments.append(("recovery_fact_pack", _safe_json(fact_pack[:5])))
    mcp_relevant_tools_raw = repo_context.get("mcp_relevant_tools", [])
    if isinstance(mcp_relevant_tools_raw, list) and mcp_relevant_tools_raw:
        working_segments.append(("mcp_relevant_tools", _safe_json(mcp_relevant_tools_raw[:5])))
    if loop_summaries:
        working_segments.append(("loop_summaries", _safe_json(loop_summaries[-3:])))
    if plan:
        working_segments.append(
            (
                "current_plan",
                _safe_json(
                    {
                        "steps": plan.get("steps", []),
                        "acceptance_criteria": plan.get("acceptance_criteria", []),
                        "max_iterations": plan.get("max_iterations", 1),
                        "recovery": plan.get("recovery"),
                        "recovery_packet": plan.get("recovery_packet"),
                    }
                ),
            )
        )
    if recent_tool_summaries:
        working_segments.append(("recent_tool_results", _safe_json(recent_tool_summaries)))
    if facts:
        working_segments.append(("recent_facts", _safe_json(facts[-5:])))
    cached_procedures_raw = repo_context.get("cached_procedures", [])
    if isinstance(cached_procedures_raw, list) and cached_procedures_raw:
        working_segments.append(("cached_procedures", _safe_json(cached_procedures_raw[:3])))

    stable_text, stable_decisions = _fit_segments(
        stable_segments,
        budget_tokens=budgets["stable_prefix_tokens"],
    )
    working_text, working_decisions = _fit_segments(
        working_segments,
        budget_tokens=budgets["working_set_tokens"],
    )
    planner_context = "\n\n".join(
        part for part in [stable_text, working_text] if part.strip()
    ).strip()
    stable_pressure = _compression_pressure(stable_decisions)
    working_pressure = _compression_pressure(working_decisions)
    overall_pressure = {
        "score": max(stable_pressure["score"], working_pressure["score"]),
        "compressed_segments": stable_pressure["compressed_segments"]
        + working_pressure["compressed_segments"],
        "dropped_segments": (
            stable_pressure["dropped_segments"] + working_pressure["dropped_segments"]
        ),
    }
    semantic_memory_count = len(semantic_memories)
    combined_fact_count = len(fact_pack) + min(semantic_memory_count, 3)

    return {
        "semantic_hits": semantic_hits,
        "stable_prefix": {
            "content": stable_text,
            "token_budget": budgets["stable_prefix_tokens"],
            "token_estimate": approx_token_count(stable_text),
        },
        "working_set": {
            "content": working_text,
            "token_budget": budgets["working_set_tokens"],
            "token_estimate": approx_token_count(working_text),
        },
        "planner_context": {
            "content": planner_context,
            "token_estimate": approx_token_count(planner_context),
            "stable_token_estimate": approx_token_count(stable_text),
            "working_set_token_estimate": approx_token_count(working_text),
            "compression_pressure": overall_pressure["score"],
            "fact_count": combined_fact_count,
            "semantic_memory_count": semantic_memory_count,
        },
        "compression": {
            "stable_prefix": stable_decisions,
            "working_set": working_decisions,
            "pressure": {
                "stable_prefix": stable_pressure,
                "working_set": working_pressure,
                "overall": overall_pressure,
            },
        },
    }


def record_compression_provenance(
    state: dict[str, Any],
    *,
    compression_result: dict[str, Any],
    current_loop: int,
) -> dict[str, Any]:
    provenance = _provenance(state)
    compression_raw = compression_result.get("compression", {})
    compression = dict(compression_raw) if isinstance(compression_raw, dict) else {}
    pressure_raw = compression.get("pressure", {})
    pressure = dict(pressure_raw) if isinstance(pressure_raw, dict) else {}
    overall_raw = pressure.get("overall", {})
    overall = dict(overall_raw) if isinstance(overall_raw, dict) else {}

    stable_decisions_raw = compression.get("stable_prefix", [])
    stable_decisions = (
        [d for d in stable_decisions_raw if isinstance(d, dict)]
        if isinstance(stable_decisions_raw, list)
        else []
    )
    working_decisions_raw = compression.get("working_set", [])
    working_decisions = (
        [d for d in working_decisions_raw if isinstance(d, dict)]
        if isinstance(working_decisions_raw, list)
        else []
    )

    compressed_segments = int(overall.get("compressed_segments", 0))
    dropped_segments = int(overall.get("dropped_segments", 0))
    pressure_score = int(overall.get("score", 0))

    provenance.append(
        {
            "event": "context_compression",
            "version": _COMPRESSION_PROVENANCE_VERSION,
            "loop": current_loop,
            "stable_prefix_compressed": sum(
                1 for d in stable_decisions if str(d.get("action", "")).strip() == "compressed"
            ),
            "stable_prefix_dropped": sum(
                1 for d in stable_decisions if str(d.get("action", "")).strip() == "dropped"
            ),
            "working_set_compressed": sum(
                1 for d in working_decisions if str(d.get("action", "")).strip() == "compressed"
            ),
            "working_set_dropped": sum(
                1 for d in working_decisions if str(d.get("action", "")).strip() == "dropped"
            ),
            "total_compressed": compressed_segments,
            "total_dropped": dropped_segments,
            "pressure_score": pressure_score,
            "stable_prefix_decisions": stable_decisions[:5],
            "working_set_decisions": working_decisions[:5],
        }
    )
    return {**_state_to_dict(state), "provenance": provenance[-20:]}


def get_compression_summary(state: dict[str, Any]) -> dict[str, Any]:
    provenance = _provenance(state)
    compression_events = [
        entry
        for entry in provenance
        if isinstance(entry, dict) and str(entry.get("event", "")).strip() == "context_compression"
    ]
    if not compression_events:
        return {"total_events": 0, "total_compressed": 0, "total_dropped": 0, "max_pressure": 0}

    total_compressed = sum(int(e.get("total_compressed", 0)) for e in compression_events)
    total_dropped = sum(int(e.get("total_dropped", 0)) for e in compression_events)
    max_pressure = max(int(e.get("pressure_score", 0)) for e in compression_events)
    loops_with_compression = [
        int(e.get("loop", 0)) for e in compression_events if int(e.get("total_compressed", 0)) > 0
    ]

    return {
        "total_events": len(compression_events),
        "total_compressed": total_compressed,
        "total_dropped": total_dropped,
        "max_pressure": max_pressure,
        "loops_with_compression": loops_with_compression,
        "last_event": compression_events[-1] if compression_events else None,
    }


def ensure_history_policy(state: object) -> dict[str, Any]:
    policy_raw = _state_get(state, "history_policy", {})
    policy_src = policy_raw if isinstance(policy_raw, dict) else {}
    normalized = _normalize_history_policy(policy_src)
    state_dict = _state_to_dict(state)
    if policy_src == normalized:
        return state_dict
    return {**state_dict, "history_policy": normalized}


def prune_pre_verification_history(state: object) -> dict[str, Any]:
    state_dict: dict[str, Any] = ensure_history_policy(state)
    policy = state_dict.get("history_policy", {})
    if not isinstance(policy, dict):
        return state

    retain_recent = _as_int(
        policy.get("retain_recent_tool_results"),
        default=_HISTORY_POLICY_DEFAULTS["retain_recent_tool_results"],
        minimum=5,
        maximum=500,
    )

    tool_results = _tool_results(state_dict)
    if len(tool_results) <= retain_recent:
        return state_dict

    dropped = len(tool_results) - retain_recent
    kept = tool_results[-retain_recent:]
    provenance = _provenance(state_dict)
    provenance.append(
        {
            "event": "tool_result_window_trim",
            "reason": "sliding_window_pre_verification",
            "dropped": dropped,
            "kept": retain_recent,
        }
    )
    return {**state_dict, "tool_results": kept, "provenance": provenance}


def prune_post_verification_history(state: object) -> dict[str, Any]:
    state_dict: dict[str, Any] = ensure_history_policy(state)
    verification = state_dict.get("verification", {})
    if not isinstance(verification, dict):
        return state_dict
    if bool(verification.get("ok", False)) is not True:
        return state_dict

    tool_results = _tool_results(state_dict)
    has_verified_apply_patch = any(
        str(result.get("tool", "")).strip() == "apply_patch" and bool(result.get("ok", False))
        for result in tool_results
    )
    if not has_verified_apply_patch:
        return state_dict

    policy = state_dict.get("history_policy", {})
    if not isinstance(policy, dict):
        return state_dict
    threshold = _as_int(
        policy.get("read_file_prune_threshold_chars"),
        default=_HISTORY_POLICY_DEFAULTS["read_file_prune_threshold_chars"],
        minimum=200,
        maximum=200_000,
    )

    updated: list[dict[str, Any]] = []
    provenance = _provenance(state_dict)
    pruned_any = False

    for index, result in enumerate(tool_results):
        tool_name = str(result.get("tool", "")).strip()
        stdout = result.get("stdout", "")
        if (
            tool_name == "read_file"
            and isinstance(stdout, str)
            and len(stdout) >= threshold
            and not stdout.startswith(_PRUNED_READ_FILE_PREFIX)
        ):
            digest = sha256(stdout.encode("utf-8", errors="replace")).hexdigest()
            artifacts_raw = result.get("artifacts", {})
            artifacts = dict(artifacts_raw) if isinstance(artifacts_raw, dict) else {}
            path_value = artifacts.get("path")
            path = str(path_value) if isinstance(path_value, str) else ""

            compact = dict(result)
            compact["stdout"] = f"{_PRUNED_READ_FILE_PREFIX} chars={len(stdout)} sha256={digest}"
            artifacts["pruned"] = {
                "reason": "post_verify_after_apply_patch",
                "stdout_chars": len(stdout),
                "stdout_sha256": digest,
            }
            compact["artifacts"] = artifacts
            updated.append(compact)

            provenance.append(
                {
                    "event": "read_file_payload_evicted",
                    "reason": "post_verify_after_apply_patch",
                    "tool_index": index,
                    "path": path,
                    "stdout_chars": len(stdout),
                    "stdout_sha256": digest,
                }
            )
            pruned_any = True
            continue

        updated.append(result)

    if not pruned_any:
        return state_dict
    return {**state_dict, "tool_results": updated, "provenance": provenance}


__all__ = [
    "HistoryPolicy",
    "approx_token_count",
    "build_context_layers",
    "context_budget_settings",
    "dedupe_semantic_hits",
    "ensure_history_policy",
    "get_compression_summary",
    "prune_post_verification_history",
    "prune_pre_verification_history",
    "record_compression_provenance",
    "summarize_tool_result",
]
