"""Smoke tests for meta-graph multi-repo orchestration."""

from __future__ import annotations

import asyncio

import pytest

from lg_orch.meta_graph import (
    DependencyGraph,
    DependencyPatch,
    MetaGraphScheduler,
    MetaRunResult,
    SubAgentTask,
    run_meta_graph,
)

# ---------------------------------------------------------------------------
# Import verification
# ---------------------------------------------------------------------------


def test_imports_succeed() -> None:
    """All meta-graph public symbols are importable."""
    assert SubAgentTask is not None
    assert DependencyGraph is not None
    assert DependencyPatch is not None
    assert MetaGraphScheduler is not None
    assert MetaRunResult is not None
    assert run_meta_graph is not None


# ---------------------------------------------------------------------------
# MetaGraph instantiation with mock repos
# ---------------------------------------------------------------------------


def _mock_tasks(n: int = 3) -> list[SubAgentTask]:
    """Create N independent tasks with no dependencies."""
    return [
        SubAgentTask(
            task_id=f"repo-{i}",
            description=f"Mock repo task {i}",
            depends_on=[],
            input_state={"repo": f"https://github.com/org/repo-{i}"},
        )
        for i in range(n)
    ]


def test_instantiate_with_mock_repos() -> None:
    """MetaGraphScheduler can be created with mock repo tasks."""
    tasks = _mock_tasks(3)
    scheduler = MetaGraphScheduler(tasks, max_parallel=2)
    assert len(scheduler.tasks) == 3
    assert all(t.status == "pending" for t in scheduler.tasks)


def test_dependency_graph_construction() -> None:
    """DependencyGraph accepts well-formed task lists."""
    tasks = _mock_tasks(3)
    dag = DependencyGraph(tasks)
    ready = dag.ready_tasks(completed_ids=set(), failed_ids=set())
    assert len(ready) == 3  # all independent → all ready


# ---------------------------------------------------------------------------
# DAG validation — no cycles, valid edges
# ---------------------------------------------------------------------------


def test_dag_rejects_cycles() -> None:
    """A cycle in the dependency graph raises ValueError."""
    tasks = [
        SubAgentTask(task_id="a", description="A", depends_on=["b"], input_state={}),
        SubAgentTask(task_id="b", description="B", depends_on=["a"], input_state={}),
    ]
    with pytest.raises(ValueError, match=r"[Cc]ycle"):
        DependencyGraph(tasks)


def test_dag_rejects_unknown_dependency() -> None:
    """A reference to an unknown task_id raises ValueError."""
    tasks = [
        SubAgentTask(task_id="a", description="A", depends_on=["missing"], input_state={}),
    ]
    with pytest.raises(ValueError, match="unknown dependency"):
        DependencyGraph(tasks)


def test_dag_rejects_duplicate_ids() -> None:
    """Duplicate task_ids are rejected."""
    tasks = [
        SubAgentTask(task_id="a", description="A1", depends_on=[], input_state={}),
        SubAgentTask(task_id="a", description="A2", depends_on=[], input_state={}),
    ]
    with pytest.raises(ValueError, match=r"[Dd]uplicate"):
        DependencyGraph(tasks)


def test_dag_valid_edges() -> None:
    """A linear chain A→B→C has correct ready ordering."""
    tasks = [
        SubAgentTask(task_id="a", description="A", depends_on=[], input_state={}),
        SubAgentTask(task_id="b", description="B", depends_on=["a"], input_state={}),
        SubAgentTask(task_id="c", description="C", depends_on=["b"], input_state={}),
    ]
    dag = DependencyGraph(tasks)

    # Only A is initially ready
    ready = dag.ready_tasks(completed_ids=set(), failed_ids=set())
    assert [t.task_id for t in ready] == ["a"]

    # Simulate A completing (mark status so ready_tasks skips it)
    tasks[0].status = "success"
    ready = dag.ready_tasks(completed_ids={"a"}, failed_ids=set())
    assert [t.task_id for t in ready] == ["b"]

    # Simulate B completing
    tasks[1].status = "success"
    ready = dag.ready_tasks(completed_ids={"a", "b"}, failed_ids=set())
    assert [t.task_id for t in ready] == ["c"]


def test_dag_add_edge_detects_cycle() -> None:
    """Adding an edge that creates a cycle is rejected."""
    tasks = [
        SubAgentTask(task_id="a", description="A", depends_on=[], input_state={}),
        SubAgentTask(task_id="b", description="B", depends_on=["a"], input_state={}),
    ]
    dag = DependencyGraph(tasks)
    with pytest.raises(ValueError, match=r"[Cc]ycle"):
        dag.add_edge("b", "a")  # would create a→b→a cycle


def test_dag_remove_edge() -> None:
    """Removing an edge changes the ready set."""
    tasks = [
        SubAgentTask(task_id="a", description="A", depends_on=[], input_state={}),
        SubAgentTask(task_id="b", description="B", depends_on=["a"], input_state={}),
    ]
    dag = DependencyGraph(tasks)
    assert len(dag.ready_tasks(set(), set())) == 1  # only A

    dag.remove_edge("a", "b")  # B no longer depends on A
    assert len(dag.ready_tasks(set(), set())) == 2  # both ready


# ---------------------------------------------------------------------------
# Parallel execution structure
# ---------------------------------------------------------------------------


def test_parallel_independent_tasks_all_ready() -> None:
    """Independent tasks are all ready simultaneously (max parallelism)."""
    tasks = _mock_tasks(5)
    dag = DependencyGraph(tasks)
    ready = dag.ready_tasks(completed_ids=set(), failed_ids=set())
    assert len(ready) == 5


def test_diamond_dag_parallelism() -> None:
    """A diamond DAG (A→B,C→D) allows B and C to run in parallel."""
    tasks = [
        SubAgentTask(task_id="a", description="start", depends_on=[], input_state={}),
        SubAgentTask(task_id="b", description="left", depends_on=["a"], input_state={}),
        SubAgentTask(task_id="c", description="right", depends_on=["a"], input_state={}),
        SubAgentTask(task_id="d", description="end", depends_on=["b", "c"], input_state={}),
    ]
    dag = DependencyGraph(tasks)

    # Initially only A
    ready = dag.ready_tasks(set(), set())
    assert [t.task_id for t in ready] == ["a"]

    # After A: B and C in parallel
    tasks[0].status = "success"
    ready = dag.ready_tasks({"a"}, set())
    ids = {t.task_id for t in ready}
    assert ids == {"b", "c"}

    # After A, B, C: D
    tasks[1].status = "success"
    tasks[2].status = "success"
    ready = dag.ready_tasks({"a", "b", "c"}, set())
    assert [t.task_id for t in ready] == ["d"]


def test_all_done_when_dependency_failed() -> None:
    """A task blocked by a failed dependency is treated as done."""
    tasks = [
        SubAgentTask(task_id="a", description="A", depends_on=[], input_state={}),
        SubAgentTask(task_id="b", description="B", depends_on=["a"], input_state={}),
    ]
    dag = DependencyGraph(tasks)
    # Mark A as failed (status != pending), B still pending but blocked
    tasks[0].status = "failed"
    assert dag.all_done(completed_ids=set(), failed_ids={"a"}) is True


def test_not_done_when_tasks_can_still_run() -> None:
    """all_done is False when tasks can still be launched."""
    tasks = [
        SubAgentTask(task_id="a", description="A", depends_on=[], input_state={}),
        SubAgentTask(task_id="b", description="B", depends_on=["a"], input_state={}),
    ]
    dag = DependencyGraph(tasks)
    assert dag.all_done(completed_ids=set(), failed_ids=set()) is False


# ---------------------------------------------------------------------------
# DependencyPatch
# ---------------------------------------------------------------------------


def test_dependency_patch_dataclass() -> None:
    """DependencyPatch has expected fields with proper defaults."""
    patch = DependencyPatch()
    assert patch.add_edges == []
    assert patch.remove_edges == []

    patch2 = DependencyPatch(add_edges=[("a", "b")], remove_edges=[("c", "d")])
    assert len(patch2.add_edges) == 1
    assert len(patch2.remove_edges) == 1


def test_dag_clone_independence() -> None:
    """Cloned graph is independent — mutations don't affect original."""
    tasks = [
        SubAgentTask(task_id="a", description="A", depends_on=[], input_state={}),
        SubAgentTask(task_id="b", description="B", depends_on=["a"], input_state={}),
    ]
    dag = DependencyGraph(tasks)
    clone = dag.clone()

    clone.remove_edge("a", "b")
    # Original still has the edge
    orig_ready = dag.ready_tasks(set(), set())
    clone_ready = clone.ready_tasks(set(), set())
    assert len(orig_ready) == 1  # only A
    assert len(clone_ready) == 2  # both A and B


# ---------------------------------------------------------------------------
# MetaRunResult
# ---------------------------------------------------------------------------


def test_meta_run_result_all_succeeded() -> None:
    """all_succeeded is True only when no failures or skips."""
    result = MetaRunResult(tasks=[], total_duration_s=1.0, succeeded=3, failed=0, skipped=0)
    assert result.all_succeeded is True

    result2 = MetaRunResult(tasks=[], total_duration_s=1.0, succeeded=2, failed=1, skipped=0)
    assert result2.all_succeeded is False

    result3 = MetaRunResult(tasks=[], total_duration_s=1.0, succeeded=2, failed=0, skipped=1)
    assert result3.all_succeeded is False


# ---------------------------------------------------------------------------
# Async run with mock graph (no real LLM)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_meta_graph_all_succeed() -> None:
    """All tasks succeed when run_graph returns immediately."""

    async def mock_run_graph(state: dict) -> dict:
        return {**state, "result": "ok"}

    tasks = _mock_tasks(3)
    result = await run_meta_graph(tasks, mock_run_graph, max_parallel=2)
    assert result.succeeded == 3
    assert result.failed == 0
    assert result.skipped == 0
    assert result.all_succeeded


@pytest.mark.asyncio
async def test_run_meta_graph_fail_fast() -> None:
    """Fail-fast aborts remaining tasks on first failure."""

    async def mock_run_graph(state: dict) -> dict:
        if state.get("repo") == "https://github.com/org/repo-0":
            raise RuntimeError("simulated failure")
        return {**state, "result": "ok"}

    tasks = _mock_tasks(3)
    result = await run_meta_graph(tasks, mock_run_graph, max_parallel=1, fail_fast=True)
    assert result.failed >= 1
    # With fail_fast, remaining tasks should be skipped
    assert result.failed + result.skipped + result.succeeded == 3


@pytest.mark.asyncio
async def test_run_meta_graph_chain_dependency() -> None:
    """Tasks in a chain execute in dependency order."""
    execution_order: list[str] = []

    async def mock_run_graph(state: dict) -> dict:
        execution_order.append(state["task_id"])
        await asyncio.sleep(0.01)
        return {**state, "result": "ok"}

    tasks = [
        SubAgentTask(task_id="a", description="A", depends_on=[], input_state={"task_id": "a"}),
        SubAgentTask(task_id="b", description="B", depends_on=["a"], input_state={"task_id": "b"}),
        SubAgentTask(task_id="c", description="C", depends_on=["b"], input_state={"task_id": "c"}),
    ]
    result = await run_meta_graph(tasks, mock_run_graph, max_parallel=4)
    assert result.all_succeeded
    assert execution_order == ["a", "b", "c"]
