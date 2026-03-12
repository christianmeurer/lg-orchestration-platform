from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


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


def load_tasks(tasks_dir: Path) -> list[EvalTask]:
    tasks: list[EvalTask] = []
    for path in sorted(tasks_dir.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        tasks.append(
            EvalTask(
                id=str(data["id"]),
                request=str(data["request"]),
                expected_intent=str(data["expected_intent"]),
                expected_halt_reason=str(data.get("expected_halt_reason", "")),
                require_final=bool(data.get("require_final", True)),
            )
        )
    return tasks


def run_task(task: EvalTask, *, repo_root: Path) -> dict[str, Any]:
    _ensure_py_src_on_path()
    from lg_orch.graph import build_graph

    app = build_graph()
    output = app.invoke(
        {
            "request": task.request,
            "_repo_root": str(repo_root),
            "_runner_base_url": "http://127.0.0.1:8088",
            "_runner_enabled": False,
            "_budget_max_loops": 1,
            "_config_policy": {
                "network_default": "deny",
                "require_approval_for_mutations": True,
                "allowed_write_paths": [],
            },
        }
    )
    return dict(output)


def score_task(task: EvalTask, output: dict[str, Any]) -> dict[str, Any]:
    actual_intent = str(output.get("intent", "")).strip()
    halt_reason = str(output.get("halt_reason", "")).strip()
    final_present = bool(str(output.get("final", "")).strip())
    tool_results_raw = output.get("tool_results", [])
    tool_results = tool_results_raw if isinstance(tool_results_raw, list) else []

    checks = {
        "intent_match": actual_intent == task.expected_intent,
        "halt_reason_match": halt_reason == task.expected_halt_reason,
        "final_present": final_present if task.require_final else True,
    }
    passed_checks = sum(1 for ok in checks.values() if ok)
    max_checks = len(checks)
    score = passed_checks / max_checks if max_checks > 0 else 0.0

    return {
        "id": task.id,
        "request": task.request,
        "expected_intent": task.expected_intent,
        "actual_intent": actual_intent,
        "expected_halt_reason": task.expected_halt_reason,
        "actual_halt_reason": halt_reason,
        "final_present": final_present,
        "tool_results_count": len(tool_results),
        "checks": checks,
        "score": score,
        "passed": passed_checks == max_checks,
    }


def evaluate_tasks(
    tasks: list[EvalTask],
    *,
    repo_root: Path,
    evaluator: Callable[[EvalTask], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    run = evaluator if evaluator is not None else (lambda task: run_task(task, repo_root=repo_root))
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

    return {
        "summary": {
            "total_tasks": total,
            "passed_tasks": passed,
            "failed_tasks": total - passed,
            "pass_rate": passed / total if total else 0.0,
            "intent_accuracy": intent_matches / total if total else 0.0,
            "average_score": avg_score,
            "average_tool_results": avg_tool_results,
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
            f"intent_accuracy={float(summary.get('intent_accuracy', 0.0)):.2f} "
            f"avg_score={float(summary.get('average_score', 0.0)):.2f}"
        )
    ]
    for result in results:
        if not isinstance(result, dict):
            continue
        status = "PASS" if bool(result.get("passed", False)) else "FAIL"
        halt_reason = str(result.get("actual_halt_reason", "")).strip() or "(none)"
        lines.append(
            (
                f"[{status}] {str(result.get('id', ''))} "
                f"score={float(result.get('score', 0.0)):.2f} "
                f"intent={str(result.get('actual_intent', '')) or '(missing)'} "
                f"halt={halt_reason} "
                f"tools={int(result.get('tool_results_count', 0))}"
            )
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="eval-run")
    parser.add_argument("--tasks-dir", default=str(Path(__file__).parent / "tasks"))
    parser.add_argument("--format", choices=["text", "json"], default="text")
    args = parser.parse_args(argv)

    tasks = load_tasks(Path(str(args.tasks_dir)))
    if not tasks:
        raise SystemExit("no tasks")
    for task in tasks:
        if not task.id or not task.request or not task.expected_intent:
            raise SystemExit(f"invalid task: {task}")

    report = evaluate_tasks(tasks, repo_root=_repo_root())
    if str(args.format) == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(_render_text_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
