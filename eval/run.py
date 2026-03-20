# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Christian Meurer — https://github.com/christianmeurer/Lula
from __future__ import annotations

import argparse
import json
import logging
import math
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

_log = logging.getLogger(__name__)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def pass_at_k(n: int, c: int, k: int) -> float:
    """Unbiased pass@k estimator.

    Args:
        n: Total number of samples drawn.
        c: Number of correct samples.
        k: Target k value.

    Returns:
        Probability that at least one of k samples is correct.

    Raises:
        ValueError: When k > n.
    """
    if k > n:
        raise ValueError(f"k ({k}) must not exceed n ({n})")
    if c == 0:
        return 0.0
    if c >= n:
        return 1.0
    denom = math.comb(n, k)
    if denom == 0:
        return 0.0
    return 1.0 - math.comb(n - c, k) / denom


def _ensure_py_src_on_path() -> None:
    py_src = _repo_root() / "py" / "src"
    py_src_text = str(py_src)
    if py_src_text not in sys.path:
        sys.path.insert(0, py_src_text)


@dataclass(frozen=True)
class EvalTask:
    id: str
    request: str
    expected_intent: str
    expected_halt_reason: str = ""
    require_final: bool = True
    expected_acceptance_ok: bool = True
    budget_max_loops: int = 1
    expected_recovery_packet_present: bool = False
    description: str = ""
    acceptance_criteria: list[str] = field(default_factory=list)
    expected_tool_calls: list[str] = field(default_factory=list)
    benchmark_class: str = ""
    difficulty: str = ""
    target_file: str = ""
    target_function: str = ""
    expected_status: str = ""
    expected_pending_approval: bool | None = None
    expected_checkpoint_present: bool | None = None
    expected_approval_history_present: bool | None = None


def load_tasks(
    tasks_dir: Path,
    task_filter: list[str] | None = None,
) -> list[EvalTask]:
    """Load eval tasks from *tasks_dir*.

    Args:
        tasks_dir: Directory containing task JSON files.
        task_filter: Optional list of task-slug strings (file stems, with
            underscores normalised to hyphens).  When provided only files
            whose normalised stem appears in the list are loaded.
    """
    tasks: list[EvalTask] = []

    def _normalise(stem: str) -> str:
        return stem.replace("_", "-").lower()

    normalised_filter = {_normalise(f) for f in task_filter} if task_filter else None

    def _build_task(task_data: dict[str, Any]) -> EvalTask:
        # Apply defaults for fields that may be absent in multi-task inner dicts.
        task_data.setdefault("expected_intent", "code_change")
        task_data.setdefault("expected_halt_reason", "")
        task_data.setdefault("require_final", False)
        task_data.setdefault("expected_acceptance_ok", True)
        task_data.setdefault("budget_max_loops", 1)
        task_data.setdefault("expected_recovery_packet_present", False)
        task_data.setdefault("description", "")
        task_data.setdefault("acceptance_criteria", [])
        task_data.setdefault("expected_tool_calls", [])
        task_data.setdefault("benchmark_class", "")
        task_data.setdefault("difficulty", "")
        task_data.setdefault("target_file", "")
        task_data.setdefault("target_function", "")
        task_data.setdefault("expected_status", "")
        expected_pending_approval_raw = task_data.get("expected_pending_approval")
        expected_checkpoint_present_raw = task_data.get("expected_checkpoint_present")
        expected_approval_history_present_raw = task_data.get("expected_approval_history_present")
        return EvalTask(
            id=str(task_data["id"]),
            request=str(task_data["request"]),
            expected_intent=str(task_data["expected_intent"]),
            expected_halt_reason=str(task_data.get("expected_halt_reason", "")),
            require_final=bool(task_data.get("require_final", True)),
            expected_acceptance_ok=bool(task_data.get("expected_acceptance_ok", True)),
            budget_max_loops=int(task_data.get("budget_max_loops", 1)),
            expected_recovery_packet_present=bool(task_data.get("expected_recovery_packet_present", False)),
            description=str(task_data.get("description", "")),
            acceptance_criteria=list(task_data.get("acceptance_criteria", [])),
            expected_tool_calls=list(task_data.get("expected_tool_calls", [])),
            benchmark_class=str(task_data.get("benchmark_class", "")),
            difficulty=str(task_data.get("difficulty", "")),
            target_file=str(task_data.get("target_file", "")),
            target_function=str(task_data.get("target_function", "")),
            expected_status=str(task_data.get("expected_status", "")),
            expected_pending_approval=(
                expected_pending_approval_raw
                if isinstance(expected_pending_approval_raw, bool)
                else None
            ),
            expected_checkpoint_present=(
                expected_checkpoint_present_raw
                if isinstance(expected_checkpoint_present_raw, bool)
                else None
            ),
            expected_approval_history_present=(
                expected_approval_history_present_raw
                if isinstance(expected_approval_history_present_raw, bool)
                else None
            ),
        )

    for path in sorted(tasks_dir.glob("*.json")):
        if normalised_filter is not None and _normalise(path.stem) not in normalised_filter:
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        # Multi-task format: top-level "tasks" array with no top-level id/request.
        if "tasks" in data and isinstance(data["tasks"], list) and "id" not in data:
            for inner in data["tasks"]:
                if not isinstance(inner, dict) or "id" not in inner:
                    _log.warning("skipping inner task in %s: missing 'id'", path)
                    continue
                tasks.append(_build_task(inner))
            continue
        if "id" not in data:
            _log.warning("skipping %s: no 'id' field and not a multi-task file", path)
            continue
        tasks.append(_build_task(data))
    return tasks


def load_swe_bench_tasks(
    path: str,
    *,
    limit: int | None = None,
) -> list[EvalTask]:
    """Load SWE-bench format task definitions from a JSONL file.

    Each line must be a JSON object with fields:
    ``instance_id``, ``repo``, ``problem_statement``, ``base_commit``,
    ``patch``, ``test_patch``, ``FAIL_TO_PASS``, ``PASS_TO_PASS``.

    Args:
        path: Path to the JSONL file containing SWE-bench instances.
        limit: If provided, only the first *limit* instances are returned.

    Returns:
        List of :class:`EvalTask` objects mapped from SWE-bench instances.
    """
    tasks: list[EvalTask] = []
    source = Path(path)
    with source.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            instance: dict[str, Any] = json.loads(line)
            instance_id: str = str(instance.get("instance_id", ""))
            problem_statement: str = str(instance.get("problem_statement", ""))
            task_text: str = f"Fix the issue described in: {problem_statement}"
            if len(task_text) > 500:
                task_text = task_text[:500]
            tasks.append(
                EvalTask(
                    id=instance_id,
                    description=problem_statement,
                    request=task_text,
                    expected_intent="code_change",
                    expected_acceptance_ok=True,
                    expected_halt_reason="accepted",
                    benchmark_class="swe_bench",
                    difficulty="hard",
                )
            )
            if limit is not None and len(tasks) >= limit:
                break
    return tasks


def run_task(
    task: EvalTask,
    *,
    repo_root: Path,
    runner_enabled: bool = False,
    temperature: float = 0.0,
) -> dict[str, Any]:
    _ensure_py_src_on_path()
    from lg_orch.graph import build_graph

    app = build_graph()
    output = app.invoke(
        {
            "request": task.request,
            "_repo_root": str(repo_root),
            "_runner_base_url": "http://127.0.0.1:8088",
            "_runner_enabled": runner_enabled,
            "_budget_max_loops": task.budget_max_loops,
            "_temperature": temperature,
            "_config_policy": {
                "network_default": "deny",
                "require_approval_for_mutations": True,
                "allowed_write_paths": [],
            },
        }
    )
    return dict(output)


# ---------------------------------------------------------------------------
# Golden file support
# ---------------------------------------------------------------------------

_NUMERIC_SUFFIX_RE = re.compile(r"-\d+$")


def load_golden(task_id: str) -> dict[str, Any] | None:
    """Load the golden assertion file for *task_id*, or ``None`` if absent.

    The golden directory lives at ``eval/golden/`` next to this file.  Task IDs
    that carry a numeric instance suffix (e.g. ``test-repair-001``) are
    normalised to their base form (``test-repair``) before lookup.
    """
    base_id = _NUMERIC_SUFFIX_RE.sub("", task_id)
    golden_path = Path(__file__).resolve().parent / "golden" / f"{base_id}.json"
    if not golden_path.exists():
        return None
    try:
        return json.loads(golden_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        _log.warning("golden file %s is not valid JSON: %s", golden_path, exc)
        return None


def _get_nested(obj: Any, path: str) -> Any:
    """Resolve a dotted *path* against *obj*, returning ``None`` on missing keys."""
    parts = path.split(".")
    current = obj
    for part in parts:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def evaluate_golden_assertions(
    result: dict[str, Any],
    golden: dict[str, Any],
) -> tuple[int, int, list[str]]:
    """Evaluate all assertions in *golden* against *result*.

    Returns ``(passed_count, total_count, failure_messages)``.

    Assertion operators:
    - ``eq``       — ``actual == value``
    - ``ne``       — ``actual != value``
    - ``lte``      — ``actual <= value`` (numeric)
    - ``gte``      — ``actual >= value`` (numeric)
    - ``in``       — ``actual in value`` (scalar in list)
    - ``contains`` — ``value in actual`` (list/string containment)
    """
    assertions = golden.get("assertions", [])
    if not isinstance(assertions, list):
        return 0, 0, []

    passed = 0
    failures: list[str] = []

    for assertion in assertions:
        if not isinstance(assertion, dict):
            continue
        # Support both "path" (spec) and "field" (existing files)
        field_key: str = str(assertion.get("path") or assertion.get("field") or "")
        op: str = str(assertion.get("op", "")).strip()
        expected: Any = assertion.get("value")

        if not field_key or not op:
            continue

        actual = _get_nested(result, field_key)

        ok: bool
        if op == "eq":
            ok = actual == expected
        elif op == "ne":
            ok = actual != expected
        elif op == "lte":
            try:
                ok = float(actual) <= float(expected)
            except (TypeError, ValueError):
                ok = False
        elif op == "gte":
            try:
                ok = float(actual) >= float(expected)
            except (TypeError, ValueError):
                ok = False
        elif op == "in":
            try:
                ok = actual in expected
            except TypeError:
                ok = False
        elif op == "contains":
            try:
                ok = expected in actual
            except TypeError:
                ok = False
        else:
            _log.warning("unknown golden assertion operator %r — skipping", op)
            continue

        if ok:
            passed += 1
        else:
            failures.append(
                f"golden assertion failed: {field_key} {op} {expected!r} (actual={actual!r})"
            )

    total = passed + len(failures)
    return passed, total, failures


def _score_tool_call_coverage(task: EvalTask, output: dict[str, Any]) -> bool:
    if not task.expected_tool_calls:
        return True
    tool_results_raw = output.get("tool_results", [])
    tool_results = tool_results_raw if isinstance(tool_results_raw, list) else []
    used_tools = {str(r.get("tool", "")).strip() for r in tool_results if isinstance(r, dict)}
    return all(t in used_tools for t in task.expected_tool_calls)


def _score_status_match(task: EvalTask, output: dict[str, Any]) -> bool:
    if not task.expected_status:
        return True
    return str(output.get("status", "")).strip() == task.expected_status


def _score_pending_approval(task: EvalTask, output: dict[str, Any]) -> bool:
    if task.expected_pending_approval is None:
        return True
    return bool(output.get("pending_approval", False)) == task.expected_pending_approval


def _score_checkpoint_presence(task: EvalTask, output: dict[str, Any]) -> bool:
    if task.expected_checkpoint_present is None:
        return True
    checkpoint_id = str(output.get("checkpoint_id", "")).strip()
    if not checkpoint_id:
        checkpoint_raw = output.get("checkpoint", {})
        checkpoint = dict(checkpoint_raw) if isinstance(checkpoint_raw, dict) else {}
        checkpoint_id = str(
            checkpoint.get("latest_checkpoint_id") or checkpoint.get("resume_checkpoint_id") or ""
        ).strip()
    return bool(checkpoint_id) == task.expected_checkpoint_present


def _score_approval_history(task: EvalTask, output: dict[str, Any]) -> bool:
    if task.expected_approval_history_present is None:
        return True
    history_raw = output.get("approval_history")
    if not isinstance(history_raw, list):
        approval_raw = output.get("approval", {})
        approval = dict(approval_raw) if isinstance(approval_raw, dict) else {}
        history_raw = approval.get("history", [])
    present = isinstance(history_raw, list) and len(history_raw) > 0
    return present == task.expected_approval_history_present


def _score_recovery_packet(task: EvalTask, output: dict[str, Any]) -> bool:
    packet = output.get("recovery_packet")
    present = isinstance(packet, dict) and bool(packet)
    return present == task.expected_recovery_packet_present


def _score_loop_summary_quality(output: dict[str, Any]) -> bool:
    verification = output.get("verification", {})
    if isinstance(verification, dict) and bool(verification.get("ok", False)):
        return True
    loop_summaries = output.get("loop_summaries", [])
    return isinstance(loop_summaries, list) and len(loop_summaries) > 0


def _score_acceptance_criteria_tracking(output: dict[str, Any]) -> bool:
    loop_summaries_raw = output.get("loop_summaries", [])
    loop_summaries = loop_summaries_raw if isinstance(loop_summaries_raw, list) else []
    if not loop_summaries:
        verification = output.get("verification", {})
        return isinstance(verification, dict) and bool(verification.get("ok", False))
    for summary in loop_summaries:
        if not isinstance(summary, dict):
            continue
        criteria = summary.get("acceptance_criteria")
        if isinstance(criteria, list) and len(criteria) > 0:
            return True
    return False


def _score_failure_fingerprint_present(output: dict[str, Any]) -> bool:
    verification = output.get("verification", {})
    if isinstance(verification, dict) and bool(verification.get("ok", False)):
        return True
    loop_summaries_raw = output.get("loop_summaries", [])
    loop_summaries = loop_summaries_raw if isinstance(loop_summaries_raw, list) else []
    for summary in loop_summaries:
        if not isinstance(summary, dict):
            continue
        fingerprint = str(summary.get("failure_fingerprint", "")).strip()
        if fingerprint and fingerprint != "verification_failed":
            return True
    return False


def _score_compression_tracking(output: dict[str, Any]) -> bool:
    telemetry_raw = output.get("telemetry", {})
    telemetry = dict(telemetry_raw) if isinstance(telemetry_raw, dict) else {}
    compression_summary = telemetry.get("compression_summary", {})
    if not isinstance(compression_summary, dict):
        return False
    total_events = int(compression_summary.get("total_events", 0))
    return total_events > 0


def score_task(task: EvalTask, output: dict[str, Any]) -> dict[str, Any]:
    actual_intent = str(output.get("intent", "")).strip()
    halt_reason = str(output.get("halt_reason", "")).strip()
    final_present = bool(str(output.get("final", "")).strip())
    actual_status = str(output.get("status", "")).strip()
    tool_results_raw = output.get("tool_results", [])
    tool_results = tool_results_raw if isinstance(tool_results_raw, list) else []
    verification_raw = output.get("verification", {})
    verification = dict(verification_raw) if isinstance(verification_raw, dict) else {}
    acceptance_ok = bool(verification.get("acceptance_ok", False))

    checks = {
        "intent_match": actual_intent == task.expected_intent,
        "halt_reason_match": halt_reason == task.expected_halt_reason,
        "final_present": final_present if task.require_final else True,
        "acceptance_ok_match": acceptance_ok == task.expected_acceptance_ok,
        "recovery_packet_match": _score_recovery_packet(task, output),
        "loop_summary_quality": _score_loop_summary_quality(output),
        "route_lane_set": bool(str(output.get("route", {}).get("lane", "")).strip()),
        "acceptance_criteria_tracking": _score_acceptance_criteria_tracking(output),
        "failure_fingerprint_present": _score_failure_fingerprint_present(output),
        "compression_tracking": _score_compression_tracking(output),
        "tool_call_coverage": _score_tool_call_coverage(task, output),
        "status_match": _score_status_match(task, output),
        "pending_approval_match": _score_pending_approval(task, output),
        "checkpoint_presence_match": _score_checkpoint_presence(task, output),
        "approval_history_match": _score_approval_history(task, output),
    }
    passed_checks = sum(1 for ok in checks.values() if ok)
    max_checks = len(checks)
    behavioral_all_passed = passed_checks == max_checks
    score = passed_checks / max_checks if max_checks > 0 else 0.0

    # Golden assertions
    golden = load_golden(task.id)
    golden_assertions_passed: int = 0
    golden_assertions_total: int = 0
    golden_assertion_failures: list[str] = []
    golden_passed: bool = True

    if golden is not None:
        golden_assertions_passed, golden_assertions_total, golden_assertion_failures = (
            evaluate_golden_assertions(output, golden)
        )
        golden_passed = len(golden_assertion_failures) == 0

    task_failures: list[str] = list(golden_assertion_failures)

    return {
        "id": task.id,
        "request": task.request,
        "expected_intent": task.expected_intent,
        "actual_intent": actual_intent,
        "expected_halt_reason": task.expected_halt_reason,
        "actual_halt_reason": halt_reason,
        "expected_status": task.expected_status,
        "actual_status": actual_status,
        "final_present": final_present,
        "acceptance_ok": acceptance_ok,
        "tool_results_count": len(tool_results),
        "checks": checks,
        "score": score,
        "golden_assertions_passed": golden_assertions_passed,
        "golden_assertions_total": golden_assertions_total,
        "golden_assertion_failures": task_failures,
        "golden_passed": golden_passed,
        "passed": behavioral_all_passed and golden_passed,
    }


def evaluate_tasks(
    tasks: list[EvalTask],
    *,
    repo_root: Path,
    evaluator: Callable[[EvalTask], dict[str, Any]] | None = None,
    runner_enabled: bool = False,
    temperature: float = 0.0,
) -> dict[str, Any]:
    if evaluator is not None:
        run = evaluator
    else:
        def run(task: EvalTask) -> dict[str, Any]:
            return run_task(task, repo_root=repo_root, runner_enabled=runner_enabled, temperature=temperature)

    results = [score_task(task, run(task)) for task in tasks]

    total = len(results)
    passed = sum(1 for result in results if bool(result.get("passed", False)))
    avg_score = sum(float(result.get("score", 0.0)) for result in results) / total if total else 0.0
    intent_matches = sum(
        1
        for result in results
        if bool(result.get("checks", {}).get("intent_match", False))
    )
    avg_tool_results = (
        sum(int(result.get("tool_results_count", 0)) for result in results) / total if total else 0.0
    )
    recovery_packet_accuracy = sum(
        1 for result in results
        if bool(result.get("checks", {}).get("recovery_packet_match", False))
    ) / total if total else 0.0
    loop_summary_quality = sum(
        1 for result in results
        if bool(result.get("checks", {}).get("loop_summary_quality", False))
    ) / total if total else 0.0
    acceptance_criteria_tracking = sum(
        1 for result in results
        if bool(result.get("checks", {}).get("acceptance_criteria_tracking", False))
    ) / total if total else 0.0
    failure_fingerprint_present = sum(
        1 for result in results
        if bool(result.get("checks", {}).get("failure_fingerprint_present", False))
    ) / total if total else 0.0
    compression_tracking = sum(
        1 for result in results
        if bool(result.get("checks", {}).get("compression_tracking", False))
    ) / total if total else 0.0
    tool_call_coverage = sum(
        1 for result in results
        if bool(result.get("checks", {}).get("tool_call_coverage", False))
    ) / total if total else 0.0
    status_accuracy = sum(
        1 for result in results
        if bool(result.get("checks", {}).get("status_match", False))
    ) / total if total else 0.0
    pending_approval_accuracy = sum(
        1 for result in results
        if bool(result.get("checks", {}).get("pending_approval_match", False))
    ) / total if total else 0.0
    checkpoint_presence_accuracy = sum(
        1 for result in results
        if bool(result.get("checks", {}).get("checkpoint_presence_match", False))
    ) / total if total else 0.0
    approval_history_accuracy = sum(
        1 for result in results
        if bool(result.get("checks", {}).get("approval_history_match", False))
    ) / total if total else 0.0

    resolved_rate: float = (
        sum(1 for r in results if bool(r.get("acceptance_ok", False))) / total
        if total
        else 0.0
    )

    return {
        "summary": {
            "total_tasks": total,
            "passed_tasks": passed,
            "failed_tasks": total - passed,
            "pass_rate": passed / total if total else 0.0,
            "resolved_rate": resolved_rate,
            "intent_accuracy": intent_matches / total if total else 0.0,
            "average_score": avg_score,
            "average_tool_results": avg_tool_results,
            "recovery_packet_accuracy": recovery_packet_accuracy,
            "loop_summary_quality": loop_summary_quality,
            "acceptance_criteria_tracking": acceptance_criteria_tracking,
            "failure_fingerprint_present": failure_fingerprint_present,
            "compression_tracking": compression_tracking,
            "tool_call_coverage": tool_call_coverage,
            "status_accuracy": status_accuracy,
            "pending_approval_accuracy": pending_approval_accuracy,
            "checkpoint_presence_accuracy": checkpoint_presence_accuracy,
            "approval_history_accuracy": approval_history_accuracy,
        },
        "results": results,
    }


def _render_text_report(report: dict[str, Any]) -> str:
    summary_raw = report.get("summary", {})
    summary = summary_raw if isinstance(summary_raw, dict) else {}
    results_raw = report.get("results", [])
    results = results_raw if isinstance(results_raw, list) else []

    lines = [
        (
            "summary: "
            f"passed={int(summary.get('passed_tasks', 0))}/{int(summary.get('total_tasks', 0))} "
            f"pass_rate={float(summary.get('pass_rate', 0.0)):.2f} "
            f"resolved_rate={float(summary.get('resolved_rate', 0.0)):.2f} "
            f"intent_accuracy={float(summary.get('intent_accuracy', 0.0)):.2f} "
            f"avg_score={float(summary.get('average_score', 0.0)):.2f} "
            f"recovery_packet_acc={float(summary.get('recovery_packet_accuracy', 0.0)):.2f} "
            f"loop_summary_quality={float(summary.get('loop_summary_quality', 0.0)):.2f} "
            f"acceptance_criteria_track={float(summary.get('acceptance_criteria_tracking', 0.0)):.2f} "
            f"failure_fingerprint={float(summary.get('failure_fingerprint_present', 0.0)):.2f} "
            f"compression_track={float(summary.get('compression_tracking', 0.0)):.2f} "
            f"tool_call_coverage={float(summary.get('tool_call_coverage', 0.0)):.2f} "
            f"status_acc={float(summary.get('status_accuracy', 0.0)):.2f} "
            f"pending_approval_acc={float(summary.get('pending_approval_accuracy', 0.0)):.2f} "
            f"checkpoint_presence_acc={float(summary.get('checkpoint_presence_accuracy', 0.0)):.2f} "
            f"approval_history_acc={float(summary.get('approval_history_accuracy', 0.0)):.2f}"
        )
    ]

    # Group results by benchmark_class for structured output.
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for result in results:
        if not isinstance(result, dict):
            continue
        bclass = str(result.get("benchmark_class", "")) or "default"
        groups[bclass].append(result)

    def _fmt_result(result: dict[str, Any]) -> str:
        status = "PASS" if bool(result.get("passed", False)) else "FAIL"
        halt_reason = str(result.get("actual_halt_reason", "")).strip() or "(none)"
        return (
            f"  [{status}] {str(result.get('id', ''))} "
            f"score={float(result.get('score', 0.0)):.2f} "
            f"intent={str(result.get('actual_intent', '')) or '(missing)'} "
            f"halt={halt_reason} "
            f"acceptance_ok={bool(result.get('acceptance_ok', False))} "
            f"tools={int(result.get('tool_results_count', 0))}"
        )

    for group_name, group_results in sorted(groups.items()):
        group_passed = sum(1 for r in group_results if bool(r.get("passed", False)))
        group_total = len(group_results)
        lines.append(f"\n[{group_name}] subtotal: {group_passed}/{group_total}")
        for result in group_results:
            lines.append(_fmt_result(result))

    return "\n".join(lines)


def _render_pass_at_k_table(rows: list[dict[str, Any]], k: int) -> str:
    """Render a structured pass@k summary table grouped by benchmark_class."""
    col_task = max((len(str(r["task"])) for r in rows), default=4)
    col_task = max(col_task, len("AGGREGATE"), len("task"))
    header = f"{'task':<{col_task}} | {'runs':>4} | {'correct':>7} | {'pass@k':>7}"
    sep = "-" * len(header)
    lines: list[str] = []

    # Group rows by benchmark_class (may be absent — default to empty string).
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        bclass = str(row.get("benchmark_class", "")) or "default"
        groups[bclass].append(row)

    def _render_row(row: dict[str, Any]) -> str:
        return (
            f"{str(row['task']):<{col_task}} | {int(row['runs']):>4} | "
            f"{int(row['correct']):>7} | {float(row['pass_at_k']):>7.3f}"
        )

    def _subtotal_row(label: str, group_rows: list[dict[str, Any]]) -> str:
        g_runs = sum(int(r["runs"]) for r in group_rows)
        g_correct = sum(int(r["correct"]) for r in group_rows)
        g_pak = pass_at_k(g_runs, g_correct, min(k, g_runs)) if g_runs else 0.0
        return (
            f"{label:<{col_task}} | {g_runs:>4} | "
            f"{g_correct:>7} | {g_pak:>7.3f}"
        )

    for group_name, group_rows in sorted(groups.items()):
        lines.append(f"\n# {group_name}")
        lines.append(header)
        lines.append(sep)
        for row in group_rows:
            lines.append(_render_row(row))
        if len(group_rows) > 1:
            lines.append(sep)
            lines.append(_subtotal_row(f"{group_name.upper()}_TOTAL", group_rows))

    # Overall aggregate.
    if len(rows) > 1:
        total_runs = sum(int(r["runs"]) for r in rows)
        total_correct = sum(int(r["correct"]) for r in rows)
        agg_pak = pass_at_k(total_runs, total_correct, min(k, total_runs))
        lines.append(f"\n{sep}")
        lines.append(
            f"{'AGGREGATE':<{col_task}} | {total_runs:>4} | "
            f"{total_correct:>7} | {agg_pak:>7.3f}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="eval-run")
    parser.add_argument("--tasks-dir", default=str(Path(__file__).parent / "tasks"))
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument(
        "--tasks",
        nargs="*",
        metavar="TASK_SLUG",
        help="Whitelist of task slugs (file stems) to run. Omit to run all.",
    )
    parser.add_argument(
        "--pass-at-k",
        dest="pass_at_k",
        type=int,
        default=1,
        metavar="K",
        help="Run each task K times and compute unbiased pass@k score.",
    )
    parser.add_argument(
        "--runner-enabled",
        dest="runner_enabled",
        action="store_true",
        default=False,
        help="Override _runner_enabled: false in task definitions to true.",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=None,
        metavar="FLOAT",
        help="Sampling temperature passed to inference. Defaults to 0.8 when --pass-at-k > 1.",
    )
    parser.add_argument(
        "--swe-bench",
        dest="swe_bench",
        default=None,
        metavar="PATH",
        help="Path to a SWE-bench JSONL file. Tasks are appended to any tasks loaded from --tasks-dir.",
    )
    parser.add_argument(
        "--swe-bench-limit",
        dest="swe_bench_limit",
        type=int,
        default=None,
        metavar="N",
        help="Limit the number of SWE-bench instances loaded.",
    )
    parser.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=False,
        help="Load all tasks, print the task list, and exit without running any graph.",
    )
    args = parser.parse_args(argv)

    task_filter: list[str] | None = args.tasks if args.tasks else None
    tasks: list[EvalTask] = []

    # Only load from tasks-dir when no explicit --swe-bench-only scenario is
    # intended; always merge both sources when both are provided.
    tasks.extend(load_tasks(Path(str(args.tasks_dir)), task_filter=task_filter))

    if args.swe_bench:
        swe_limit: int | None = int(args.swe_bench_limit) if args.swe_bench_limit is not None else None
        tasks.extend(load_swe_bench_tasks(str(args.swe_bench), limit=swe_limit))

    if not tasks:
        raise SystemExit("no tasks")
    for task in tasks:
        if not task.id or not task.request or not task.expected_intent:
            raise SystemExit(f"invalid task: {task}")

    if args.dry_run:
        for task in tasks:
            print(f"task: {task.id}  benchmark_class={task.benchmark_class or '(none)'}  difficulty={task.difficulty or '(none)'}")
        return 0

    k: int = int(args.pass_at_k)
    runner_enabled: bool = bool(args.runner_enabled)

    # Auto-set temperature when pass@k > 1 and user did not explicitly set it.
    if args.temperature is not None:
        temperature: float = float(args.temperature)
    elif k > 1:
        temperature = 0.8
    else:
        temperature = 0.0

    if k <= 1:
        # Standard single-run path — preserves all existing behaviour.
        report = evaluate_tasks(
            tasks,
            repo_root=_repo_root(),
            runner_enabled=runner_enabled,
            temperature=temperature,
        )
        summary = report.get("summary", {})
        resolved_rate = float(summary.get("resolved_rate", 0.0))
        if str(args.format) == "json":
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(_render_text_report(report))
            print(f"resolved_rate={resolved_rate:.3f}")
        return 0

    # pass@k multi-run path.
    pak_rows: list[dict[str, Any]] = []
    all_reports: list[dict[str, Any]] = []

    for task in tasks:
        run_results: list[dict[str, Any]] = []
        for _ in range(k):
            output = run_task(
                task,
                repo_root=_repo_root(),
                runner_enabled=runner_enabled,
                temperature=temperature,
            )
            run_results.append(score_task(task, output))

        n_correct = sum(1 for r in run_results if bool(r.get("passed", False)))
        pak_score = pass_at_k(k, n_correct, k)
        pak_rows.append(
            {
                "task": task.id,
                "runs": k,
                "correct": n_correct,
                "pass_at_k": pak_score,
                "benchmark_class": task.benchmark_class,
            }
        )
        all_reports.extend(run_results)

    resolved_rate_pak = (
        sum(1 for r in all_reports if bool(r.get("acceptance_ok", False))) / len(all_reports)
        if all_reports
        else 0.0
    )
    if str(args.format) == "json":
        print(
            json.dumps(
                {
                    "pass_at_k_rows": pak_rows,
                    "results": all_reports,
                    "resolved_rate": resolved_rate_pak,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(_render_pass_at_k_table(pak_rows, k))
        print(f"resolved_rate={resolved_rate_pak:.3f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
