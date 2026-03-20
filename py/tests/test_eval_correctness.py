from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch


def _load_eval_run_module() -> Any:
    repo_root = Path(__file__).resolve().parents[2]
    module_path = repo_root / "eval" / "run.py"
    spec = importlib.util.spec_from_file_location("repo_eval_run_correctness", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# pass_at_k — pure unit tests
# ---------------------------------------------------------------------------


def test_pass_at_k_all_correct() -> None:
    module = _load_eval_run_module()
    assert module.pass_at_k(n=5, c=5, k=1) == 1.0


def test_pass_at_k_none_correct() -> None:
    module = _load_eval_run_module()
    assert module.pass_at_k(n=5, c=0, k=1) == 0.0


def test_pass_at_k_partial_correct_k1() -> None:
    module = _load_eval_run_module()
    # With n=5, c=3, k=1: 1 - C(2,1)/C(5,1) = 1 - 2/5 = 0.6
    result = module.pass_at_k(n=5, c=3, k=1)
    assert abs(result - 0.6) < 1e-9


def test_pass_at_k_large_k() -> None:
    module = _load_eval_run_module()
    result = module.pass_at_k(n=10, c=5, k=5)
    assert 0.0 < result < 1.0


def test_pass_at_k_k_equals_n() -> None:
    module = _load_eval_run_module()
    # k == n is permitted; at least one must be correct when c > 0
    result = module.pass_at_k(n=5, c=3, k=5)
    assert 0.0 < result <= 1.0


def test_pass_at_k_raises_when_k_exceeds_n() -> None:
    import pytest

    module = _load_eval_run_module()
    with pytest.raises(ValueError, match="k"):
        module.pass_at_k(n=4, c=2, k=5)


def test_pass_at_k_k1_one_of_one_correct() -> None:
    module = _load_eval_run_module()
    assert module.pass_at_k(n=1, c=1, k=1) == 1.0


def test_pass_at_k_k1_zero_of_one_correct() -> None:
    module = _load_eval_run_module()
    assert module.pass_at_k(n=1, c=0, k=1) == 0.0


# ---------------------------------------------------------------------------
# CLI — --pass-at-k 1 runs each task once
# ---------------------------------------------------------------------------


def test_cli_pass_at_k_1_runs_once(tmp_path: Path, capsys: object) -> None:
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

    call_count = 0

    def _fake_run_task(task: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        return {
            "intent": "analysis",
            "halt_reason": "",
            "final": "done",
            "tool_results": [],
            "verification": {"acceptance_ok": True, "ok": True},
            "route": {"lane": "fast"},
            "telemetry": {"compression_summary": {"total_events": 1}},
        }

    original = module.run_task
    module.run_task = _fake_run_task
    try:
        rc = module.main(["--tasks-dir", str(tmp_path), "--pass-at-k", "1"])
    finally:
        module.run_task = original

    assert rc == 0
    assert call_count == 1


# ---------------------------------------------------------------------------
# CLI — --runner-enabled propagates to run_task
# ---------------------------------------------------------------------------


def test_cli_runner_enabled_flag(tmp_path: Path) -> None:
    module = _load_eval_run_module()

    task_path = tmp_path / "canary.json"
    task_path.write_text(
        json.dumps(
            {
                "id": "canary-001",
                "request": "Summarize.",
                "expected_intent": "analysis",
            }
        ),
        encoding="utf-8",
    )

    captured_kwargs: dict[str, Any] = {}

    def _fake_run_task(task: Any, **kwargs: Any) -> dict[str, Any]:
        captured_kwargs.update(kwargs)
        return {
            "intent": "analysis",
            "halt_reason": "",
            "final": "done",
            "tool_results": [],
            "verification": {"acceptance_ok": True, "ok": True},
            "route": {"lane": "fast"},
            "telemetry": {"compression_summary": {"total_events": 1}},
        }

    original = module.run_task
    module.run_task = _fake_run_task
    try:
        rc = module.main(["--tasks-dir", str(tmp_path), "--runner-enabled"])
    finally:
        module.run_task = original

    assert rc == 0
    assert captured_kwargs.get("runner_enabled") is True


# ---------------------------------------------------------------------------
# CLI — --temperature propagates to run_task
# ---------------------------------------------------------------------------


def test_cli_temperature_propagates(tmp_path: Path) -> None:
    module = _load_eval_run_module()

    task_path = tmp_path / "canary.json"
    task_path.write_text(
        json.dumps(
            {
                "id": "canary-001",
                "request": "Summarize.",
                "expected_intent": "analysis",
            }
        ),
        encoding="utf-8",
    )

    captured_kwargs: dict[str, Any] = {}

    def _fake_run_task(task: Any, **kwargs: Any) -> dict[str, Any]:
        captured_kwargs.update(kwargs)
        return {
            "intent": "analysis",
            "halt_reason": "",
            "final": "done",
            "tool_results": [],
            "verification": {"acceptance_ok": True, "ok": True},
            "route": {"lane": "fast"},
            "telemetry": {"compression_summary": {"total_events": 1}},
        }

    original = module.run_task
    module.run_task = _fake_run_task
    try:
        rc = module.main(["--tasks-dir", str(tmp_path), "--temperature", "0.5"])
    finally:
        module.run_task = original

    assert rc == 0
    assert abs(float(captured_kwargs.get("temperature", -1)) - 0.5) < 1e-9


# ---------------------------------------------------------------------------
# CLI — --pass-at-k K > 1 auto-sets temperature to 0.8
# ---------------------------------------------------------------------------


def test_cli_pass_at_k_gt1_auto_temperature(tmp_path: Path, capsys: object) -> None:
    module = _load_eval_run_module()

    task_path = tmp_path / "canary.json"
    task_path.write_text(
        json.dumps(
            {
                "id": "canary-001",
                "request": "Summarize.",
                "expected_intent": "analysis",
            }
        ),
        encoding="utf-8",
    )

    captured_temps: list[float] = []

    def _fake_run_task(task: Any, **kwargs: Any) -> dict[str, Any]:
        captured_temps.append(float(kwargs.get("temperature", -1.0)))
        return {
            "intent": "analysis",
            "halt_reason": "",
            "final": "done",
            "tool_results": [],
            "verification": {"acceptance_ok": True, "ok": True},
            "route": {"lane": "fast"},
            "telemetry": {"compression_summary": {"total_events": 1}},
        }

    original = module.run_task
    module.run_task = _fake_run_task
    try:
        rc = module.main(["--tasks-dir", str(tmp_path), "--pass-at-k", "3"])
    finally:
        module.run_task = original

    assert rc == 0
    # Called 3 times (k=3), all with temperature=0.8
    assert len(captured_temps) == 3
    for t in captured_temps:
        assert abs(t - 0.8) < 1e-9


# ---------------------------------------------------------------------------
# CLI — --pass-at-k K > 1 prints structured table
# ---------------------------------------------------------------------------


def test_cli_pass_at_k_table_output(tmp_path: Path, capsys: object) -> None:
    module = _load_eval_run_module()

    task_path = tmp_path / "canary.json"
    task_path.write_text(
        json.dumps(
            {
                "id": "canary-001",
                "request": "Summarize.",
                "expected_intent": "analysis",
            }
        ),
        encoding="utf-8",
    )

    def _fake_run_task(task: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "intent": "analysis",
            "halt_reason": "",
            "final": "done",
            "tool_results": [],
            "verification": {"acceptance_ok": True, "ok": True},
            "route": {"lane": "fast"},
            "telemetry": {"compression_summary": {"total_events": 1}},
        }

    original = module.run_task
    module.run_task = _fake_run_task
    try:
        rc = module.main(["--tasks-dir", str(tmp_path), "--pass-at-k", "2"])
    finally:
        module.run_task = original

    assert rc == 0
    out = capsys.readouterr().out
    assert "pass@k" in out
    assert "canary-001" in out
