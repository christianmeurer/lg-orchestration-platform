from __future__ import annotations

import importlib.util
import json
import sys
import textwrap
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
            "loop_count": 1,
            "tool_results": [],
            "verification": {"acceptance_ok": True, "ok": True},
            "route": {"lane": "interactive"},
            "telemetry": {"compression_summary": {"total_events": 1}},
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
        "a": {
            "intent": "analysis",
            "halt_reason": "",
            "final": "done",
            "tool_results": [],
            "verification": {"acceptance_ok": True, "ok": True},
            "route": {"lane": "fast"},
            "telemetry": {"compression_summary": {"total_events": 1}},
        },
        "b": {
            "intent": "analysis",
            "halt_reason": "",
            "final": "done",
            "tool_results": [{}],
            "verification": {"acceptance_ok": False},
            "route": {"lane": "deep"},
        },
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
    module.run_task = lambda task, repo_root, **kwargs: {
        "intent": "analysis",
        "halt_reason": "",
        "final": "done",
        "loop_count": 1,
        "tool_results": [],
        "verification": {"acceptance_ok": True, "ok": True},
        "route": {"lane": "interactive"},
        "telemetry": {"compression_summary": {"total_events": 1}},
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


def test_score_task_tracks_acceptance_status() -> None:
    module = _load_eval_run_module()
    task = module.EvalTask(
        id="canary",
        request="summarize repo",
        expected_intent="analysis",
        expected_acceptance_ok=False,
    )
    result = module.score_task(
        task,
        {
            "intent": "analysis",
            "halt_reason": "",
            "final": "done",
            "tool_results": [],
            "verification": {"acceptance_ok": False},
        },
    )
    assert result["checks"]["acceptance_ok_match"] is True
    assert result["acceptance_ok"] is False


# --- Wave 5 new tests ---


def test_load_tasks_parses_new_fields(tmp_path: Path) -> None:
    module = _load_eval_run_module()
    task_path = tmp_path / "test-task.json"
    task_path.write_text(
        json.dumps(
            {
                "id": "test-001",
                "request": "Do something.",
                "expected_intent": "analysis",
                "budget_max_loops": 3,
                "expected_recovery_packet_present": True,
                "description": "A test task description.",
                "expected_status": "suspended",
                "expected_pending_approval": True,
                "expected_checkpoint_present": True,
                "expected_approval_history_present": True,
            }
        ),
        encoding="utf-8",
    )
    tasks = module.load_tasks(tmp_path)
    assert len(tasks) == 1
    t = tasks[0]
    assert t.budget_max_loops == 3
    assert t.expected_recovery_packet_present is True
    assert t.description == "A test task description."
    assert t.expected_status == "suspended"
    assert t.expected_pending_approval is True
    assert t.expected_checkpoint_present is True
    assert t.expected_approval_history_present is True


def test_load_tasks_new_fields_defaults(tmp_path: Path) -> None:
    module = _load_eval_run_module()
    task_path = tmp_path / "minimal.json"
    task_path.write_text(
        json.dumps(
            {
                "id": "minimal-001",
                "request": "Summarize.",
                "expected_intent": "analysis",
            }
        ),
        encoding="utf-8",
    )
    tasks = module.load_tasks(tmp_path)
    assert tasks[0].budget_max_loops == 1
    assert tasks[0].expected_recovery_packet_present is False
    assert tasks[0].description == ""
    assert tasks[0].expected_status == ""
    assert tasks[0].expected_pending_approval is None
    assert tasks[0].expected_checkpoint_present is None
    assert tasks[0].expected_approval_history_present is None


def test_score_recovery_packet_present_and_expected() -> None:
    module = _load_eval_run_module()
    task = module.EvalTask(
        id="rp",
        request="fix it",
        expected_intent="code_change",
        expected_recovery_packet_present=True,
    )
    output = {"recovery_packet": {"error": "compile failed", "hint": "check line 5"}}
    assert module._score_recovery_packet(task, output) is True


def test_score_recovery_packet_absent_and_not_expected() -> None:
    module = _load_eval_run_module()
    task = module.EvalTask(
        id="rp",
        request="summarize",
        expected_intent="analysis",
        expected_recovery_packet_present=False,
    )
    assert module._score_recovery_packet(task, {}) is True


def test_score_recovery_packet_present_but_not_expected() -> None:
    module = _load_eval_run_module()
    task = module.EvalTask(
        id="rp",
        request="summarize",
        expected_intent="analysis",
        expected_recovery_packet_present=False,
    )
    output = {"recovery_packet": {"error": "unexpected"}}
    assert module._score_recovery_packet(task, output) is False


def test_score_recovery_packet_absent_but_expected() -> None:
    module = _load_eval_run_module()
    task = module.EvalTask(
        id="rp",
        request="fix it",
        expected_intent="code_change",
        expected_recovery_packet_present=True,
    )
    assert module._score_recovery_packet(task, {}) is False


def test_score_loop_summary_quality_verification_ok() -> None:
    module = _load_eval_run_module()
    output = {"verification": {"ok": True}}
    assert module._score_loop_summary_quality(output) is True


def test_score_loop_summary_quality_nonempty_summaries() -> None:
    module = _load_eval_run_module()
    output = {"loop_summaries": ["attempt 1 failed: timeout"]}
    assert module._score_loop_summary_quality(output) is True


def test_score_loop_summary_quality_neither() -> None:
    module = _load_eval_run_module()
    output: dict = {}
    assert module._score_loop_summary_quality(output) is False


def test_score_task_has_seven_checks() -> None:
    module = _load_eval_run_module()
    task = module.EvalTask(
        id="t",
        request="r",
        expected_intent="analysis",
        expected_acceptance_ok=False,
        require_final=False,
    )
    result = module.score_task(
        task,
        {
            "intent": "analysis",
            "halt_reason": "",
            "final": "",
            "tool_results": [],
            "verification": {"acceptance_ok": False},
        },
    )
    assert len(result["checks"]) == 15


def test_evaluate_tasks_summary_has_recovery_keys() -> None:
    module = _load_eval_run_module()
    task = module.EvalTask(
        id="x",
        request="do",
        expected_intent="analysis",
        expected_acceptance_ok=False,
        require_final=False,
        expected_recovery_packet_present=False,
    )
    output = {
        "intent": "analysis",
        "halt_reason": "",
        "final": "",
        "tool_results": [],
        "verification": {"acceptance_ok": False},
        "loop_summaries": ["attempt 1"],
    }
    report = module.evaluate_tasks(
        [task],
        repo_root=Path("."),
        evaluator=lambda _t: output,
    )
    assert "recovery_packet_accuracy" in report["summary"]
    assert "loop_summary_quality" in report["summary"]
    assert "status_accuracy" in report["summary"]
    assert "pending_approval_accuracy" in report["summary"]
    assert "checkpoint_presence_accuracy" in report["summary"]
    assert "approval_history_accuracy" in report["summary"]
    assert report["summary"]["loop_summary_quality"] == 1.0
    assert report["summary"]["recovery_packet_accuracy"] == 1.0


def test_score_task_tracks_approval_control_plane_fields() -> None:
    module = _load_eval_run_module()
    task = module.EvalTask(
        id="approval",
        request="approve the run",
        expected_intent="analysis",
        expected_status="suspended",
        expected_pending_approval=True,
        expected_checkpoint_present=True,
        expected_approval_history_present=True,
    )
    result = module.score_task(
        task,
        {
            "intent": "analysis",
            "halt_reason": "",
            "final": "done",
            "status": "suspended",
            "pending_approval": True,
            "checkpoint_id": "cp-123",
            "approval_history": [{"decision": "approved"}],
            "tool_results": [],
            "verification": {"acceptance_ok": True, "ok": True},
            "route": {"lane": "interactive"},
            "telemetry": {"compression_summary": {"total_events": 1}},
        },
    )
    assert result["checks"]["status_match"] is True
    assert result["checks"]["pending_approval_match"] is True
    assert result["checks"]["checkpoint_presence_match"] is True
    assert result["checks"]["approval_history_match"] is True


# --- Wave 8 new tests ---

_SWE_BENCH_FIXTURE = textwrap.dedent("""\
    {"instance_id": "astropy__astropy-1234", "repo": "astropy/astropy", "problem_statement": "Calling foo() raises ValueError when bar is None.", "base_commit": "abc123", "patch": "diff --git a/foo.py", "test_patch": "diff --git a/test_foo.py", "FAIL_TO_PASS": ["test_foo"], "PASS_TO_PASS": []}
    {"instance_id": "django__django-5678", "repo": "django/django", "problem_statement": "Migration crashes on PostgreSQL when table has no rows.", "base_commit": "def456", "patch": "diff --git a/db.py", "test_patch": "diff --git a/test_db.py", "FAIL_TO_PASS": ["test_db"], "PASS_TO_PASS": ["test_models"]}
""")


def test_load_swe_bench_tasks_basic(tmp_path: Path) -> None:
    module = _load_eval_run_module()
    jsonl_file = tmp_path / "swe_bench.jsonl"
    jsonl_file.write_text(_SWE_BENCH_FIXTURE, encoding="utf-8")

    tasks = module.load_swe_bench_tasks(str(jsonl_file))

    assert len(tasks) == 2
    t0 = tasks[0]
    assert t0.id == "astropy__astropy-1234"
    assert t0.expected_intent == "code_change"
    assert t0.expected_acceptance_ok is True
    assert t0.expected_halt_reason == "accepted"
    assert t0.benchmark_class == "swe_bench"
    assert t0.difficulty == "hard"
    assert "Calling foo()" in t0.request
    assert len(t0.request) <= 500

    t1 = tasks[1]
    assert t1.id == "django__django-5678"


def test_load_swe_bench_tasks_limit(tmp_path: Path) -> None:
    module = _load_eval_run_module()
    jsonl_file = tmp_path / "swe_bench.jsonl"
    jsonl_file.write_text(_SWE_BENCH_FIXTURE, encoding="utf-8")

    tasks = module.load_swe_bench_tasks(str(jsonl_file), limit=1)

    assert len(tasks) == 1
    assert tasks[0].id == "astropy__astropy-1234"


def test_load_swe_bench_tasks_truncates_task_text(tmp_path: Path) -> None:
    module = _load_eval_run_module()
    long_statement = "X" * 600
    line = json.dumps(
        {
            "instance_id": "long-id",
            "repo": "x/x",
            "problem_statement": long_statement,
            "base_commit": "a",
            "patch": "",
            "test_patch": "",
            "FAIL_TO_PASS": [],
            "PASS_TO_PASS": [],
        }
    )
    jsonl_file = tmp_path / "long.jsonl"
    jsonl_file.write_text(line + "\n", encoding="utf-8")

    tasks = module.load_swe_bench_tasks(str(jsonl_file))
    assert len(tasks[0].request) == 500


def test_resolved_rate_in_evaluate_tasks_summary() -> None:
    module = _load_eval_run_module()
    tasks = [
        module.EvalTask(id="a", request="fix it", expected_intent="code_change"),
        module.EvalTask(id="b", request="fix it too", expected_intent="code_change"),
    ]

    def _make_output(acceptance_ok: bool) -> dict:
        return {
            "intent": "code_change",
            "halt_reason": "",
            "final": "done",
            "tool_results": [],
            "verification": {"acceptance_ok": acceptance_ok, "ok": acceptance_ok},
            "route": {"lane": "fast"},
            "telemetry": {"compression_summary": {"total_events": 1}},
        }

    outputs = {"a": _make_output(True), "b": _make_output(False)}
    report = module.evaluate_tasks(
        tasks,
        repo_root=Path("."),
        evaluator=lambda t: outputs[t.id],
    )

    assert "resolved_rate" in report["summary"]
    assert report["summary"]["resolved_rate"] == 0.5


def test_resolved_rate_all_resolved() -> None:
    module = _load_eval_run_module()
    tasks = [
        module.EvalTask(id="x", request="r", expected_intent="code_change"),
        module.EvalTask(id="y", request="r", expected_intent="code_change"),
    ]
    ok_output = {
        "intent": "code_change",
        "halt_reason": "",
        "final": "done",
        "tool_results": [],
        "verification": {"acceptance_ok": True, "ok": True},
        "route": {"lane": "fast"},
        "telemetry": {"compression_summary": {"total_events": 1}},
    }
    report = module.evaluate_tasks(
        tasks,
        repo_root=Path("."),
        evaluator=lambda _t: ok_output,
    )
    assert report["summary"]["resolved_rate"] == 1.0


def test_swe_bench_flag_parsed(tmp_path: Path, capsys: object) -> None:
    module = _load_eval_run_module()
    jsonl_file = tmp_path / "swe.jsonl"
    jsonl_file.write_text(_SWE_BENCH_FIXTURE, encoding="utf-8")

    # Use --dry-run so no graph is actually invoked.
    rc = module.main(["--swe-bench", str(jsonl_file), "--dry-run"])

    assert rc == 0
    captured = capsys.readouterr()
    assert "astropy__astropy-1234" in captured.out
    assert "django__django-5678" in captured.out


def test_dry_run_exits_zero_with_task_list(tmp_path: Path, capsys: object) -> None:
    module = _load_eval_run_module()
    task_path = tmp_path / "canary.json"
    task_path.write_text(
        json.dumps(
            {
                "id": "canary-dry",
                "request": "Summarize the repository.",
                "expected_intent": "analysis",
            }
        ),
        encoding="utf-8",
    )

    rc = module.main(["--tasks-dir", str(tmp_path), "--dry-run"])

    assert rc == 0
    captured = capsys.readouterr()
    assert "canary-dry" in captured.out


def test_dry_run_does_not_invoke_graph(tmp_path: Path) -> None:
    """--dry-run must not call run_task even when tasks are present."""
    module = _load_eval_run_module()
    task_path = tmp_path / "t.json"
    task_path.write_text(
        json.dumps({"id": "t-1", "request": "do it", "expected_intent": "code_change"}),
        encoding="utf-8",
    )
    called: list[str] = []
    original = module.run_task

    def _mock_run_task(task, **kwargs):  # type: ignore[override]
        called.append(task.id)
        return {}

    module.run_task = _mock_run_task
    try:
        rc = module.main(["--tasks-dir", str(tmp_path), "--dry-run"])
    finally:
        module.run_task = original

    assert rc == 0
    assert called == [], "run_task must not be called during --dry-run"
