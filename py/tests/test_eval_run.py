from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


def _load_eval_run_module():
    repo_root = Path(__file__).resolve().parents[2]
    module_path = repo_root / "eval" / "run.py"
    spec = importlib.util.spec_from_file_location("repo_eval_run", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_score_task_passes_with_matching_output() -> None:
    module = _load_eval_run_module()
    task = module.EvalTask(id="canary", request="summarize repo", expected_intent="analysis")

    result = module.score_task(
        task,
        {
            "intent": "analysis",
            "halt_reason": "",
            "final": "done",
            "tool_results": [],
        },
    )

    assert result["passed"] is True
    assert result["score"] == 1.0
    assert result["checks"]["intent_match"] is True


def test_evaluate_tasks_aggregates_summary() -> None:
    module = _load_eval_run_module()
    tasks = [
        module.EvalTask(id="a", request="summarize repo", expected_intent="analysis"),
        module.EvalTask(id="b", request="debug failing test", expected_intent="debug"),
    ]

    outputs = {
        "a": {"intent": "analysis", "halt_reason": "", "final": "done", "tool_results": []},
        "b": {"intent": "analysis", "halt_reason": "", "final": "done", "tool_results": [{}]},
    }

    report = module.evaluate_tasks(
        tasks,
        repo_root=Path("."),
        evaluator=lambda task: outputs[task.id],
    )

    assert report["summary"]["total_tasks"] == 2
    assert report["summary"]["passed_tasks"] == 1
    assert report["summary"]["intent_accuracy"] == 0.5
    assert report["summary"]["average_tool_results"] == 0.5


def test_main_json_output_uses_scored_report(tmp_path: Path, capsys: object) -> None:
    module = _load_eval_run_module()
    task_path = tmp_path / "canary.json"
    task_path.write_text(
        json.dumps(
            {
                "id": "canary-001",
                "request": "Summarize the repository structure.",
                "expected_intent": "analysis",
            }
        ),
        encoding="utf-8",
    )

    original_run_task = module.run_task
    module.run_task = lambda task, repo_root: {
        "intent": "analysis",
        "halt_reason": "",
        "final": "done",
        "tool_results": [],
    }
    try:
        rc = module.main(["--tasks-dir", str(tmp_path), "--format", "json"])
    finally:
        module.run_task = original_run_task

    assert rc == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["summary"]["passed_tasks"] == 1
    assert payload["results"][0]["score"] == 1.0
