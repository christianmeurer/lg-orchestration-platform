"""Tests for py/src/lg_orch/meta_graph.py (Gap 3, Wave 8).

All pre-existing tests that relied on the old LangGraph StateGraph
placeholder (meta_planner, task_dispatcher, sub_agent_executor,
meta_evaluator, build_meta_graph) are replaced here because those
functions no longer exist after the rewrite.  Every scenario described
in the implementation spec is covered.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from lg_orch.meta_graph import (
    DependencyGraph,
    MetaGraphScheduler,
    MetaRunResult,
    SubAgentTask,
    run_meta_graph,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _task(
    task_id: str,
    deps: list[str] | None = None,
    extra: dict[str, Any] | None = None,
) -> SubAgentTask:
    return SubAgentTask(
        task_id=task_id,
        description=f"Task {task_id}",
        depends_on=deps or [],
        input_state={"task_id": task_id, **(extra or {})},
    )


async def _instant(state: dict[str, Any]) -> dict[str, Any]:
    """Succeeds immediately; propagates the input state as the result."""
    await asyncio.sleep(0)
    return {**state, "done": True}


async def _failing(state: dict[str, Any]) -> dict[str, Any]:
    """Always raises, simulating a sub-agent crash."""
    await asyncio.sleep(0)
    raise RuntimeError(f"simulated failure for {state.get('task_id')}")


# ---------------------------------------------------------------------------
# 1. DependencyGraph unit tests
# ---------------------------------------------------------------------------


class TestDependencyGraph:
    def test_no_duplicates_accepted(self) -> None:
        tasks = [_task("a"), _task("b")]
        dag = DependencyGraph(tasks)
        assert dag is not None

    def test_duplicate_task_ids_raise(self) -> None:
        tasks = [_task("a"), _task("a")]
        with pytest.raises(ValueError, match="Duplicate task_ids"):
            DependencyGraph(tasks)

    def test_unknown_dependency_raises(self) -> None:
        tasks = [_task("a", deps=["nonexistent"])]
        with pytest.raises(ValueError, match="unknown dependency"):
            DependencyGraph(tasks)

    def test_cycle_detected_direct(self) -> None:
        """A → B and B → A forms a two-node cycle."""
        tasks = [
            _task("a", deps=["b"]),
            _task("b", deps=["a"]),
        ]
        with pytest.raises(ValueError, match="Cycle detected in task dependency graph"):
            DependencyGraph(tasks)

    def test_cycle_detected_indirect(self) -> None:
        """A → B → C → A forms a three-node cycle."""
        tasks = [
            _task("a", deps=["c"]),
            _task("b", deps=["a"]),
            _task("c", deps=["b"]),
        ]
        with pytest.raises(ValueError, match="Cycle detected in task dependency graph"):
            DependencyGraph(tasks)

    def test_ready_tasks_empty_deps(self) -> None:
        tasks = [_task("a"), _task("b", deps=["a"])]
        dag = DependencyGraph(tasks)
        ready = dag.ready_tasks(set(), set())
        assert [t.task_id for t in ready] == ["a"]

    def test_ready_tasks_after_completion(self) -> None:
        tasks = [_task("a"), _task("b", deps=["a"])]
        dag = DependencyGraph(tasks)
        # In real usage the scheduler sets status="success" before adding to
        # completed_ids; reflect that here so ready_tasks can filter it out.
        tasks[0].status = "success"
        ready = dag.ready_tasks({"a"}, set())
        ids = [t.task_id for t in ready]
        assert "b" in ids
        assert "a" not in ids

    def test_all_done_true_when_blocked(self) -> None:
        tasks = [_task("a"), _task("b", deps=["a"])]
        dag = DependencyGraph(tasks)
        # a is failed, b depends on a — b can never run
        tasks[0].status = "failed"
        assert dag.all_done(set(), {"a"}) is True

    def test_all_done_false_when_pending_runnable(self) -> None:
        tasks = [_task("a")]
        dag = DependencyGraph(tasks)
        assert dag.all_done(set(), set()) is False


# ---------------------------------------------------------------------------
# 2. MetaRunResult unit tests
# ---------------------------------------------------------------------------


class TestMetaRunResult:
    def _make(
        self, succeeded: int, failed: int, skipped: int
    ) -> MetaRunResult:
        return MetaRunResult(
            tasks=[],
            total_duration_s=0.0,
            succeeded=succeeded,
            failed=failed,
            skipped=skipped,
        )

    def test_all_succeeded_true(self) -> None:
        assert self._make(3, 0, 0).all_succeeded is True

    def test_all_succeeded_false_on_failure(self) -> None:
        assert self._make(2, 1, 0).all_succeeded is False

    def test_all_succeeded_false_on_skip(self) -> None:
        assert self._make(2, 0, 1).all_succeeded is False

    def test_all_succeeded_false_on_both(self) -> None:
        assert self._make(1, 1, 1).all_succeeded is False

    def test_zero_tasks_all_succeeded(self) -> None:
        assert self._make(0, 0, 0).all_succeeded is True


# ---------------------------------------------------------------------------
# 3. Scenario: linear chain  (A → B)
# ---------------------------------------------------------------------------


def test_linear_chain_ab() -> None:
    """Task B must not start until A completes; both succeed in order."""
    execution_order: list[str] = []

    async def ordered_graph(state: dict[str, Any]) -> dict[str, Any]:
        execution_order.append(state["task_id"])
        return {"done": True}

    tasks = [_task("a"), _task("b", deps=["a"])]

    result = asyncio.run(run_meta_graph(tasks, ordered_graph))

    assert result.succeeded == 2
    assert result.failed == 0
    assert result.skipped == 0
    assert result.all_succeeded is True
    # A must precede B in execution order.
    assert execution_order.index("a") < execution_order.index("b")


def test_linear_chain_abc() -> None:
    """Three-task chain: A → B → C; all succeed in strict order."""
    execution_order: list[str] = []

    async def ordered_graph(state: dict[str, Any]) -> dict[str, Any]:
        execution_order.append(state["task_id"])
        return {}

    tasks = [_task("a"), _task("b", deps=["a"]), _task("c", deps=["b"])]

    result = asyncio.run(run_meta_graph(tasks, ordered_graph))

    assert result.all_succeeded is True
    for earlier, later in [("a", "b"), ("b", "c")]:
        assert execution_order.index(earlier) < execution_order.index(later)


# ---------------------------------------------------------------------------
# 4. Scenario: fan-out  (A → B, A → C run concurrently)
# ---------------------------------------------------------------------------


def test_fanout_bc_run_concurrently() -> None:
    """After A completes, B and C should overlap in execution."""
    max_concurrent: list[int] = [0]
    currently_running: set[str] = set()
    lock = asyncio.Lock()

    async def tracking_graph(state: dict[str, Any]) -> dict[str, Any]:
        tid: str = state["task_id"]
        if tid != "a":
            # B and C: record concurrency, then hold briefly.
            async with lock:
                currently_running.add(tid)
                max_concurrent[0] = max(max_concurrent[0], len(currently_running))
            await asyncio.sleep(0.05)
            async with lock:
                currently_running.discard(tid)
        return {}

    tasks = [_task("a"), _task("b", deps=["a"]), _task("c", deps=["a"])]

    result = asyncio.run(
        run_meta_graph(tasks, tracking_graph, max_parallel=4)
    )

    assert result.all_succeeded is True
    # Both B and C must have been in-flight simultaneously.
    assert max_concurrent[0] == 2, (
        f"Expected max concurrent = 2 (fan-out), got {max_concurrent[0]}"
    )


# ---------------------------------------------------------------------------
# 5. Scenario: fan-in  (A, B → C)
# ---------------------------------------------------------------------------


def test_fanin_c_starts_after_ab() -> None:
    """C must not start until both A and B have finished."""
    finished_at: dict[str, float] = {}
    started_at: dict[str, float] = {}

    async def timing_graph(state: dict[str, Any]) -> dict[str, Any]:
        tid: str = state["task_id"]
        started_at[tid] = time.monotonic()
        await asyncio.sleep(0.02)
        finished_at[tid] = time.monotonic()
        return {}

    tasks = [_task("a"), _task("b"), _task("c", deps=["a", "b"])]

    result = asyncio.run(run_meta_graph(tasks, timing_graph, max_parallel=4))

    assert result.all_succeeded is True
    # C must start after both A and B have finished.
    assert started_at["c"] >= finished_at["a"]
    assert started_at["c"] >= finished_at["b"]


# ---------------------------------------------------------------------------
# 6. Scenario: cycle detection
# ---------------------------------------------------------------------------


def test_cycle_raises_on_run() -> None:
    """Constructing the scheduler with a cyclic graph raises ValueError."""
    tasks = [_task("a", deps=["b"]), _task("b", deps=["a"])]
    with pytest.raises(ValueError, match="Cycle detected in task dependency graph"):
        asyncio.run(run_meta_graph(tasks, _instant))


# ---------------------------------------------------------------------------
# 7. Scenario: fail-fast (default)
# ---------------------------------------------------------------------------


def test_fail_fast_skips_remaining() -> None:
    """When A fails and fail_fast=True, independent B is skipped.

    asyncio scheduling reality: with max_parallel=1, when A's _launch
    releases the semaphore, B's _launch is scheduled *before* asyncio.wait
    returns — so B will have started running (status="running") by the time
    fail_fast fires.  The scheduler cancels B's asyncio Task and marks it
    "skipped".  B must have a real await so it is still suspended (and thus
    cancelable) when the scheduler gets control.
    """
    tasks = [
        _task("a"),  # will fail instantly
        _task("b"),  # independent; suspended at sleep → cancelled → skipped
    ]

    async def selective_graph(state: dict[str, Any]) -> dict[str, Any]:
        if state["task_id"] == "a":
            raise RuntimeError("task a failed")
        # B sleeps long enough that the scheduler cancels it before it finishes.
        await asyncio.sleep(10.0)
        return {}

    result = asyncio.run(
        run_meta_graph(tasks, selective_graph, fail_fast=True, max_parallel=1)
    )

    assert result.succeeded == 0
    assert result.failed == 1
    assert result.skipped == 1
    assert result.all_succeeded is False

    task_map = {t.task_id: t for t in result.tasks}
    assert task_map["a"].status == "failed"
    assert task_map["a"].error is not None
    assert task_map["b"].status == "skipped"


def test_fail_fast_dependent_task_skipped() -> None:
    """When A fails, B (which depends on A) is skipped under fail_fast."""
    tasks = [_task("a"), _task("b", deps=["a"])]

    result = asyncio.run(run_meta_graph(tasks, _failing, fail_fast=True))

    assert result.failed == 1
    assert result.skipped == 1
    task_map = {t.task_id: t for t in result.tasks}
    assert task_map["a"].status == "failed"
    assert task_map["b"].status == "skipped"


# ---------------------------------------------------------------------------
# 8. Scenario: fail-non-fast (fail_fast=False)
# ---------------------------------------------------------------------------


def test_fail_non_fast_independent_task_still_runs() -> None:
    """When A fails with fail_fast=False, independent B still completes."""
    tasks = [
        _task("a"),  # will fail
        _task("b"),  # independent; must still run and succeed
    ]

    async def selective_graph(state: dict[str, Any]) -> dict[str, Any]:
        if state["task_id"] == "a":
            raise RuntimeError("task a failed")
        return {"result": "ok"}

    result = asyncio.run(
        run_meta_graph(tasks, selective_graph, fail_fast=False)
    )

    assert result.succeeded == 1
    assert result.failed == 1
    assert result.skipped == 0

    task_map = {t.task_id: t for t in result.tasks}
    assert task_map["a"].status == "failed"
    assert task_map["b"].status == "success"
    # selective_graph returns only {"result": "ok"}; input_state is not merged
    # back by run_graph — that is the caller's responsibility.
    assert task_map["b"].result == {"result": "ok"}


def test_fail_non_fast_blocked_dependency_still_skipped() -> None:
    """Even with fail_fast=False, a task whose dep failed is skipped."""
    tasks = [_task("a"), _task("b", deps=["a"]), _task("c")]

    async def selective_graph(state: dict[str, Any]) -> dict[str, Any]:
        if state["task_id"] == "a":
            raise RuntimeError("task a error")
        return {}

    result = asyncio.run(
        run_meta_graph(tasks, selective_graph, fail_fast=False)
    )

    task_map = {t.task_id: t for t in result.tasks}
    assert task_map["a"].status == "failed"
    assert task_map["b"].status == "skipped"   # blocked by failed dep
    assert task_map["c"].status == "success"   # independent — should run


# ---------------------------------------------------------------------------
# 9. Scenario: max_parallel limits concurrency
# ---------------------------------------------------------------------------


def test_max_parallel_cap() -> None:
    """Peak concurrency must never exceed max_parallel."""
    max_parallel = 2
    peak: list[int] = [0]
    current: list[int] = [0]
    lock = asyncio.Lock()

    async def slow_graph(state: dict[str, Any]) -> dict[str, Any]:
        async with lock:
            current[0] += 1
            peak[0] = max(peak[0], current[0])
        await asyncio.sleep(0.05)
        async with lock:
            current[0] -= 1
        return {}

    # 5 independent tasks — all ready at once, but only 2 can run together.
    tasks = [_task(str(i)) for i in range(5)]

    result = asyncio.run(
        run_meta_graph(tasks, slow_graph, max_parallel=max_parallel)
    )

    assert result.all_succeeded is True
    assert peak[0] <= max_parallel, (
        f"Peak concurrency {peak[0]} exceeded max_parallel={max_parallel}"
    )


# ---------------------------------------------------------------------------
# 10. Scenario: task result and timing fields populated
# ---------------------------------------------------------------------------


def test_task_result_and_timing_fields() -> None:
    """Successful tasks must have started_at, finished_at, and result set."""

    async def result_graph(state: dict[str, Any]) -> dict[str, Any]:
        return {"answer": 42}

    tasks = [_task("x")]
    result = asyncio.run(run_meta_graph(tasks, _instant))

    t = result.tasks[0]
    assert t.status == "success"
    assert t.started_at is not None
    assert t.finished_at is not None
    assert t.finished_at >= t.started_at
    assert t.result is not None


def test_failed_task_error_field_populated() -> None:
    """Failed tasks must carry a non-empty error string."""
    tasks = [_task("bad")]
    result = asyncio.run(run_meta_graph(tasks, _failing))

    t = result.tasks[0]
    assert t.status == "failed"
    assert t.error is not None
    assert len(t.error) > 0
    assert t.finished_at is not None


# ---------------------------------------------------------------------------
# 11. Scenario: empty task list
# ---------------------------------------------------------------------------


def test_empty_task_list() -> None:
    """An empty task list should succeed immediately with zero counts."""
    result = asyncio.run(run_meta_graph([], _instant))
    assert result.succeeded == 0
    assert result.failed == 0
    assert result.skipped == 0
    assert result.all_succeeded is True


# ---------------------------------------------------------------------------
# 12. MetaGraphScheduler direct API
# ---------------------------------------------------------------------------


def test_scheduler_direct_api() -> None:
    """MetaGraphScheduler.run() returns the same result as run_meta_graph."""
    tasks = [_task("a"), _task("b", deps=["a"])]
    scheduler = MetaGraphScheduler(tasks, _instant, max_parallel=2, fail_fast=True)
    result = asyncio.run(scheduler.run())

    assert isinstance(result, MetaRunResult)
    assert result.all_succeeded is True


def test_scheduler_rejects_cycle() -> None:
    """MetaGraphScheduler.run() raises ValueError for cyclic input."""
    tasks = [_task("x", deps=["y"]), _task("y", deps=["x"])]
    scheduler = MetaGraphScheduler(tasks, _instant)
    with pytest.raises(ValueError, match="Cycle detected"):
        asyncio.run(scheduler.run())
