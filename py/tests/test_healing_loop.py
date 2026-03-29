from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from lg_orch.healing_loop import HealingJob, HealingLoop, TestSuiteResult, detect_test_runner

# ---------------------------------------------------------------------------
# poll_once tests
# ---------------------------------------------------------------------------


def _make_proc(stdout: bytes, returncode: int) -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, b""))
    return proc


def test_poll_once_parses_passed_count(tmp_path: Any) -> None:
    output = b"3 passed in 0.12s\n"

    async def _run() -> TestSuiteResult:
        with patch("asyncio.create_subprocess_exec", return_value=_make_proc(output, 0)):
            loop = HealingLoop(repo_path=str(tmp_path))
            return await loop.poll_once()

    result = asyncio.run(_run())
    assert result.passed == 3
    assert result.failed == 0
    assert isinstance(result, TestSuiteResult)


def test_poll_once_parses_failed_tests(tmp_path: Any) -> None:
    output = b"FAILED tests/test_foo.py::test_bar - AssertionError\n1 failed, 2 passed in 0.50s\n"

    async def _run() -> TestSuiteResult:
        with patch("asyncio.create_subprocess_exec", return_value=_make_proc(output, 1)):
            loop = HealingLoop(repo_path=str(tmp_path))
            return await loop.poll_once()

    result = asyncio.run(_run())
    assert result.failed_tests == ["tests/test_foo.py::test_bar"]
    assert result.failed == 1
    assert result.passed == 2


def test_poll_once_handles_subprocess_error(tmp_path: Any) -> None:
    output = b"collected 0 items / 1 error\n"

    async def _run() -> TestSuiteResult:
        with patch("asyncio.create_subprocess_exec", return_value=_make_proc(output, 2)):
            loop = HealingLoop(repo_path=str(tmp_path))
            return await loop.poll_once()

    result = asyncio.run(_run())
    assert result.errors >= 1


# ---------------------------------------------------------------------------
# HealingLoop integration tests
# ---------------------------------------------------------------------------


def test_healing_loop_creates_job_on_failure(tmp_path: Any) -> None:
    failing_result = TestSuiteResult(
        run_id="r1",
        repo_path=str(tmp_path),
        passed=0,
        failed=1,
        errors=0,
        failed_tests=["tests/test_foo.py::test_bar"],
        output="FAILED tests/test_foo.py::test_bar",
        timestamp=time.time(),
    )

    call_count = 0

    async def fake_graph_runner(payload: dict[str, Any]) -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        return {"ok": True, "verification": {"ok": True}}

    healing = HealingLoop(
        repo_path=str(tmp_path),
        poll_interval_seconds=0.0,
        max_concurrent_jobs=2,
        graph_runner=fake_graph_runner,
    )

    poll_count = 0

    async def fake_poll_once() -> TestSuiteResult:
        nonlocal poll_count
        poll_count += 1
        if poll_count == 1:
            return failing_result
        raise asyncio.CancelledError

    healing.poll_once = fake_poll_once  # type: ignore[method-assign]

    async def _run() -> None:
        import contextlib

        with contextlib.suppress(TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(healing.run_until_cancelled(), timeout=2.0)

    asyncio.run(_run())

    history = healing.get_job_history()
    assert len(history) >= 1
    assert history[0].status == "healed"
    assert call_count >= 1


def test_healing_loop_limits_concurrent_jobs(tmp_path: Any) -> None:
    concurrent_running = 0
    max_observed = 0

    async def counting_runner(payload: dict[str, Any]) -> dict[str, Any]:
        nonlocal concurrent_running, max_observed
        concurrent_running += 1
        max_observed = max(max_observed, concurrent_running)
        await asyncio.sleep(0.05)
        concurrent_running -= 1
        return {"ok": True}

    healing = HealingLoop(
        repo_path=str(tmp_path),
        poll_interval_seconds=0.0,
        max_concurrent_jobs=1,
        graph_runner=counting_runner,
    )

    # pre-populate two queued jobs
    job1 = HealingJob(
        job_id="j1",
        repo_path=str(tmp_path),
        failing_tests=["tests/test_a.py::test_1"],
        priority=1,
        created_at=time.time(),
        status="queued",
    )
    job2 = HealingJob(
        job_id="j2",
        repo_path=str(tmp_path),
        failing_tests=["tests/test_b.py::test_2"],
        priority=1,
        created_at=time.time(),
        status="queued",
    )
    healing._pending_jobs = [job1, job2]
    healing._job_history = [job1, job2]

    asyncio.run(healing._dispatch_pending_jobs())

    # with max_concurrent_jobs=1, only 1 dispatched per cycle
    assert max_observed <= 1


def test_healing_loop_stops_on_cancellation(tmp_path: Any) -> None:
    healing = HealingLoop(
        repo_path=str(tmp_path),
        poll_interval_seconds=100.0,
    )

    async def immediate_cancel() -> TestSuiteResult:
        raise asyncio.CancelledError

    healing.poll_once = immediate_cancel  # type: ignore[method-assign]

    # Should return cleanly without raising
    asyncio.run(healing.run_until_cancelled())


# ---------------------------------------------------------------------------
# detect_test_runner tests
# ---------------------------------------------------------------------------


def test_detect_test_runner_cargo(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "mylib"\nversion = "0.1.0"\n')
    assert detect_test_runner(tmp_path) == "cargo test --all"


def test_detect_test_runner_nodejs(tmp_path: Path) -> None:
    pkg = {"name": "myapp", "scripts": {"test": "jest"}}
    (tmp_path / "package.json").write_text(json.dumps(pkg))
    assert detect_test_runner(tmp_path) == "npm test"


def test_detect_test_runner_golang(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module example.com/mymod\n\ngo 1.21\n")
    assert detect_test_runner(tmp_path) == "go test ./..."


def test_detect_test_runner_python_default(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    assert detect_test_runner(tmp_path) == "python -m pytest"


def test_detect_test_runner_fallback(tmp_path: Path) -> None:
    # Empty directory — no marker files present
    assert detect_test_runner(tmp_path) == "python -m pytest"


# ---------------------------------------------------------------------------
# Fix 10.4: Typed handoff and post-healing verification
# ---------------------------------------------------------------------------


def test_run_job_sends_structured_handoff(tmp_path: Any) -> None:
    """_run_job sends a structured healing_context dict, not a formatted string."""
    captured_payload: list[dict[str, Any]] = []

    async def capturing_runner(payload: dict[str, Any]) -> dict[str, Any]:
        captured_payload.append(payload)
        return {"verification": {"ok": True}}

    healing = HealingLoop(
        repo_path=str(tmp_path),
        graph_runner=capturing_runner,
    )

    job = HealingJob(
        job_id="j-structured",
        repo_path=str(tmp_path),
        failing_tests=["tests/test_a.py::test_1", "tests/test_b.py::test_2"],
        priority=1,
        created_at=time.time(),
        status="running",
    )

    asyncio.run(healing._run_job(job))

    assert len(captured_payload) == 1
    payload = captured_payload[0]
    assert payload["task"] == "Fix failing tests"
    assert "healing_context" in payload
    ctx = payload["healing_context"]
    assert ctx["job_id"] == "j-structured"
    assert ctx["failing_tests"] == ["tests/test_a.py::test_1", "tests/test_b.py::test_2"]
    assert ctx["failure_class"] == "test_failure"
    assert ctx["repo_path"] == str(tmp_path)
    assert payload["repo_path"] == str(tmp_path)
    assert payload["healing_job_id"] == "j-structured"


def test_run_job_marks_healed_on_verification_ok(tmp_path: Any) -> None:
    """_run_job marks job as healed when verification.ok is True."""

    async def ok_runner(payload: dict[str, Any]) -> dict[str, Any]:
        return {"verification": {"ok": True}}

    healing = HealingLoop(repo_path=str(tmp_path), graph_runner=ok_runner)
    job = HealingJob(
        job_id="j-ok",
        repo_path=str(tmp_path),
        failing_tests=["tests/test_a.py::test_1"],
        priority=1,
        created_at=time.time(),
        status="running",
    )

    asyncio.run(healing._run_job(job))
    assert job.status == "healed"


def test_run_job_marks_failed_on_verification_not_ok(tmp_path: Any) -> None:
    """_run_job marks job as failed when verification.ok is False."""

    async def fail_runner(payload: dict[str, Any]) -> dict[str, Any]:
        return {"verification": {"ok": False, "failure_class": "test_assertion"}}

    healing = HealingLoop(repo_path=str(tmp_path), graph_runner=fail_runner)
    job = HealingJob(
        job_id="j-fail",
        repo_path=str(tmp_path),
        failing_tests=["tests/test_a.py::test_1"],
        priority=1,
        created_at=time.time(),
        status="running",
    )

    asyncio.run(healing._run_job(job))
    assert job.status == "failed"


def test_run_job_marks_failed_on_missing_verification(tmp_path: Any) -> None:
    """_run_job marks job as failed when result dict has no verification key."""

    async def no_verif_runner(payload: dict[str, Any]) -> dict[str, Any]:
        return {"result": "done"}

    healing = HealingLoop(repo_path=str(tmp_path), graph_runner=no_verif_runner)
    job = HealingJob(
        job_id="j-no-verif",
        repo_path=str(tmp_path),
        failing_tests=["tests/test_a.py::test_1"],
        priority=1,
        created_at=time.time(),
        status="running",
    )

    asyncio.run(healing._run_job(job))
    assert job.status == "failed"
