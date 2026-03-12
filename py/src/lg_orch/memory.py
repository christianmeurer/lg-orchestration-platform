from __future__ import annotations

import json
from hashlib import sha256
from typing import Any

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

HistoryPolicy = dict[str, int]


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


def _tool_results(state: dict[str, Any]) -> list[dict[str, Any]]:
    raw = state.get("tool_results", [])
    if not isinstance(raw, list):
        return []
    return [entry for entry in raw if isinstance(entry, dict)]


def _provenance(state: dict[str, Any]) -> list[dict[str, Any]]:
    raw = state.get("provenance", [])
    if not isinstance(raw, list):
        return []
    return [entry for entry in raw if isinstance(entry, dict)]


def approx_token_count(text: str) -> int:
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def context_budget_settings(state: dict[str, Any]) -> dict[str, int]:
    raw = state.get("_budget_context", {})
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
            continue
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
        key=lambda item: float(item.get("score", 0)) if isinstance(item.get("score"), (int, float)) else 0.0,
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
        "exit_code": int(result.get("exit_code", 0)) if isinstance(result.get("exit_code"), int) else 0,
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


def build_context_layers(
    *,
    state: dict[str, Any],
    repo_context: dict[str, Any],
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

    stable_segments: list[tuple[str, str]] = [
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
        )
    ]

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

    verification_raw = state.get("verification", {})
    verification = dict(verification_raw) if isinstance(verification_raw, dict) else {}
    plan_raw = state.get("plan", {})
    plan = dict(plan_raw) if isinstance(plan_raw, dict) else {}
    facts_raw = state.get("facts", [])
    facts = facts_raw if isinstance(facts_raw, list) else []
    loop_summaries_raw = state.get("loop_summaries", [])
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
                    }
                ),
            )
        )
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
                    }
                ),
            )
        )
    if recent_tool_summaries:
        working_segments.append(("recent_tool_results", _safe_json(recent_tool_summaries)))
    if facts:
        working_segments.append(("recent_facts", _safe_json(facts[-5:])))

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
        },
        "compression": {
            "stable_prefix": stable_decisions,
            "working_set": working_decisions,
        },
    }


def ensure_history_policy(state: dict[str, Any]) -> dict[str, Any]:
    policy_raw = state.get("history_policy", {})
    policy_src = policy_raw if isinstance(policy_raw, dict) else {}
    normalized = _normalize_history_policy(policy_src)
    if policy_src == normalized:
        return state
    return {**state, "history_policy": normalized}


def prune_pre_verification_history(state: dict[str, Any]) -> dict[str, Any]:
    state = ensure_history_policy(state)
    policy = state.get("history_policy", {})
    if not isinstance(policy, dict):
        return state

    retain_recent = _as_int(
        policy.get("retain_recent_tool_results"),
        default=_HISTORY_POLICY_DEFAULTS["retain_recent_tool_results"],
        minimum=5,
        maximum=500,
    )

    tool_results = _tool_results(state)
    if len(tool_results) <= retain_recent:
        return state

    dropped = len(tool_results) - retain_recent
    kept = tool_results[-retain_recent:]
    provenance = _provenance(state)
    provenance.append(
        {
            "event": "tool_result_window_trim",
            "reason": "sliding_window_pre_verification",
            "dropped": dropped,
            "kept": retain_recent,
        }
    )
    return {**state, "tool_results": kept, "provenance": provenance}


def prune_post_verification_history(state: dict[str, Any]) -> dict[str, Any]:
    state = ensure_history_policy(state)
    verification = state.get("verification", {})
    if not isinstance(verification, dict):
        return state
    if bool(verification.get("ok", False)) is not True:
        return state

    tool_results = _tool_results(state)
    has_verified_apply_patch = any(
        str(result.get("tool", "")).strip() == "apply_patch" and bool(result.get("ok", False))
        for result in tool_results
    )
    if not has_verified_apply_patch:
        return state

    policy = state.get("history_policy", {})
    if not isinstance(policy, dict):
        return state
    threshold = _as_int(
        policy.get("read_file_prune_threshold_chars"),
        default=_HISTORY_POLICY_DEFAULTS["read_file_prune_threshold_chars"],
        minimum=200,
        maximum=200_000,
    )

    updated: list[dict[str, Any]] = []
    provenance = _provenance(state)
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
            compact["stdout"] = (
                f"{_PRUNED_READ_FILE_PREFIX} chars={len(stdout)} sha256={digest}"
            )
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
        return state
    return {**state, "tool_results": updated, "provenance": provenance}
