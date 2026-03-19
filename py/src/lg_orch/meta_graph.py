"""Dependency-aware multi-agent scheduler (Gap 3, Wave 8).

This module is intentionally decoupled from graph.py and state.py.
Callers supply a ``run_graph`` callable that wraps the actual LangGraph
invocation; this module only handles DAG construction, scheduling,
concurrency control, and result collection.

Exported public names:
    SubAgentTask, DependencyGraph, MetaGraphScheduler, MetaRunResult,
    run_meta_graph
"""
from __future__ import annotations

import asyncio
import dataclasses
import time
from collections import deque
from typing import Any, Awaitable, Callable, Literal

import structlog

__all__ = [
    "SubAgentTask",
    "DependencyGraph",
    "MetaGraphScheduler",
    "MetaRunResult",
    "run_meta_graph",
]

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

TaskStatus = Literal["pending", "running", "success", "failed", "skipped"]


@dataclasses.dataclass
class SubAgentTask:
    """A single unit of work in the meta-graph scheduler."""

    task_id: str
    description: str
    depends_on: list[str]
    input_state: dict[str, Any]
    status: TaskStatus = "pending"
    result: dict[str, Any] | None = None
    error: str | None = None
    started_at: float | None = None
    finished_at: float | None = None


@dataclasses.dataclass
class MetaRunResult:
    """Aggregate outcome of a full meta-graph execution."""

    tasks: list[SubAgentTask]
    total_duration_s: float
    succeeded: int
    failed: int
    skipped: int

    @property
    def all_succeeded(self) -> bool:
        """True iff every task succeeded (no failures, no skips)."""
        return self.failed == 0 and self.skipped == 0


class DependencyGraph:
    """Lightweight DAG over :class:`SubAgentTask` instances.

    Validates for duplicates and cycles on construction.
    """

    def __init__(self, tasks: list[SubAgentTask]) -> None:
        task_ids = [t.task_id for t in tasks]
        if len(task_ids) != len(set(task_ids)):
            duplicates = {tid for tid in task_ids if task_ids.count(tid) > 1}
            raise ValueError(
                f"Duplicate task_ids in task list: {sorted(duplicates)}"
            )
        self._tasks = tasks
        self._id_set: set[str] = set(task_ids)
        self._validate_no_cycles()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _validate_no_cycles(self) -> None:
        """Raise ValueError if the dependency graph contains a cycle.

        Uses Kahn's topological-sort algorithm: if we cannot process all
        nodes, a cycle must exist.
        """
        # Verify that every declared dependency references a known task.
        for task in self._tasks:
            for dep in task.depends_on:
                if dep not in self._id_set:
                    raise ValueError(
                        f"Task '{task.task_id}' declares unknown dependency '{dep}'"
                    )

        # Build in-degree map and reverse-adjacency map.
        in_degree: dict[str, int] = {t.task_id: 0 for t in self._tasks}
        dependents: dict[str, list[str]] = {t.task_id: [] for t in self._tasks}
        for task in self._tasks:
            for dep in task.depends_on:
                in_degree[task.task_id] += 1
                dependents[dep].append(task.task_id)

        # Kahn's BFS.
        queue: deque[str] = deque(
            tid for tid, deg in in_degree.items() if deg == 0
        )
        processed = 0
        while queue:
            node = queue.popleft()
            processed += 1
            for dependent_id in dependents[node]:
                in_degree[dependent_id] -= 1
                if in_degree[dependent_id] == 0:
                    queue.append(dependent_id)

        if processed != len(self._tasks):
            raise ValueError("Cycle detected in task dependency graph")

    # ------------------------------------------------------------------
    # Public query methods
    # ------------------------------------------------------------------

    def ready_tasks(
        self,
        completed_ids: set[str],
        failed_ids: set[str],  # noqa: ARG002  — kept for API symmetry
    ) -> list[SubAgentTask]:
        """Return pending tasks whose every dependency has completed."""
        ready: list[SubAgentTask] = []
        for task in self._tasks:
            if task.status != "pending":
                continue
            if all(dep in completed_ids for dep in task.depends_on):
                ready.append(task)
        return ready

    def all_done(self, completed_ids: set[str], failed_ids: set[str]) -> bool:
        """True when no pending task could still be launched.

        A pending task is *launchable* iff none of its dependencies are in
        ``failed_ids`` (i.e., it is not permanently blocked).  Tasks whose
        dependencies are all completed will eventually show up in
        :meth:`ready_tasks`; tasks permanently blocked by a failed dependency
        will never run and therefore count as ``done`` from the scheduler's
        perspective.
        """
        for task in self._tasks:
            if task.status != "pending":
                continue
            # If any dep failed, this task is forever blocked → treat as done.
            # If no dep failed, this task might still run → not done yet.
            if not any(dep in failed_ids for dep in task.depends_on):
                return False
        return True


class MetaGraphScheduler:
    """Async scheduler that dispatches :class:`SubAgentTask` instances in
    dependency order with bounded parallelism.

    Args:
        tasks: All tasks to schedule.  Must form an acyclic dependency graph.
        run_graph: Async callable accepting an initial-state dict and returning
                   the final-state dict.  Typically wraps a LangGraph graph
                   invocation, but this class has no direct import of graph.py.
        max_parallel: Maximum number of sub-agent invocations running at once.
        fail_fast: When *True*, skip all remaining pending tasks and cancel
                   running ones as soon as any task fails.
    """

    def __init__(
        self,
        tasks: list[SubAgentTask],
        run_graph: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
        *,
        max_parallel: int = 4,
        fail_fast: bool = True,
    ) -> None:
        self.tasks = tasks
        self._run_graph = run_graph
        self._max_parallel = max(1, max_parallel)
        self._fail_fast = fail_fast

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def run(self) -> MetaRunResult:
        """Execute all tasks respecting dependency order.

        Scheduling loop (Python 3.11+, structured concurrency style):

        1. Build a :class:`DependencyGraph` from *self.tasks*.
        2. Repeatedly find *ready* tasks and launch them as asyncio tasks,
           limited by the semaphore to *max_parallel* concurrent invocations.
        3. After each :func:`asyncio.wait` FIRST_COMPLETED round, process
           results and update the completed / failed sets.
        4. If ``fail_fast`` is *True* and any failure occurred, mark remaining
           pending tasks as ``"skipped"``, cancel running asyncio tasks, and
           return early.
        5. When no asyncio tasks remain in flight, mark any still-pending
           (dependency-blocked) tasks as ``"skipped"`` and return.

        Returns:
            A :class:`MetaRunResult` summarising the full execution.
        """
        dag = DependencyGraph(self.tasks)
        sema = asyncio.Semaphore(self._max_parallel)
        completed_ids: set[str] = set()
        failed_ids: set[str] = set()
        started_ids: set[str] = set()
        # Maps the live asyncio.Task → the SubAgentTask it is executing.
        running: dict[asyncio.Task[None], SubAgentTask] = {}
        start = time.monotonic()

        while True:
            # ── Launch all newly-ready tasks ──────────────────────────────
            for task in dag.ready_tasks(completed_ids, failed_ids):
                if task.task_id in started_ids:
                    continue
                started_ids.add(task.task_id)

                # Default-argument capture is required inside a loop to avoid
                # the classic late-binding closure pitfall.
                async def _launch(t: SubAgentTask = task) -> None:
                    async with sema:
                        await self._run_task(t)

                asyncio_task: asyncio.Task[None] = asyncio.create_task(
                    _launch(), name=f"sub_agent_{task.task_id}"
                )
                running[asyncio_task] = task

            # ── Termination guard ─────────────────────────────────────────
            # If nothing is running, no further progress is possible.
            if not running:
                # Permanently blocked pending tasks become "skipped".
                for t in self.tasks:
                    if t.status == "pending":
                        t.status = "skipped"
                break

            # ── Wait for at least one completion ──────────────────────────
            done_set, _ = await asyncio.wait(
                list(running.keys()), return_when=asyncio.FIRST_COMPLETED
            )

            new_failures: list[SubAgentTask] = []
            for done_asyncio_task in done_set:
                sub_task = running.pop(done_asyncio_task)
                # _run_task already sets status; handle unexpected cancellation.
                if sub_task.status == "running":
                    sub_task.status = "failed"
                    sub_task.error = "cancelled before completion"
                    sub_task.finished_at = time.monotonic()
                if sub_task.status == "success":
                    completed_ids.add(sub_task.task_id)
                else:
                    failed_ids.add(sub_task.task_id)
                    new_failures.append(sub_task)

            # ── Fail-fast handling ────────────────────────────────────────
            if self._fail_fast and new_failures:
                log.warning(
                    "meta_graph.fail_fast_triggered",
                    failed_tasks=[t.task_id for t in new_failures],
                )
                # Mark every still-pending task as skipped.
                for t in self.tasks:
                    if t.status == "pending":
                        t.status = "skipped"
                # Cancel any asyncio tasks still in flight.
                for remaining in list(running.keys()):
                    remaining.cancel()
                if running:
                    await asyncio.gather(*running.keys(), return_exceptions=True)
                # Tasks left in "running" state were cancelled mid-flight by the
                # scheduler (not due to their own error).  Count them as skipped,
                # not as failures — the cancellation was a scheduler decision.
                for t in self.tasks:
                    if t.status == "running":
                        t.status = "skipped"
                running.clear()
                break

        total = time.monotonic() - start
        succeeded = sum(1 for t in self.tasks if t.status == "success")
        failed = sum(1 for t in self.tasks if t.status == "failed")
        skipped = sum(1 for t in self.tasks if t.status == "skipped")

        log.info(
            "meta_graph.run_complete",
            succeeded=succeeded,
            failed=failed,
            skipped=skipped,
            total_duration_s=round(total, 4),
        )
        return MetaRunResult(
            tasks=self.tasks,
            total_duration_s=total,
            succeeded=succeeded,
            failed=failed,
            skipped=skipped,
        )

    # ------------------------------------------------------------------
    # Internal task runner
    # ------------------------------------------------------------------

    async def _run_task(self, task: SubAgentTask) -> None:
        """Invoke ``run_graph`` for a single task and record the outcome."""
        task.status = "running"
        task.started_at = time.monotonic()
        log.info("meta_graph.task_start", task_id=task.task_id)
        try:
            final_state = await self._run_graph(task.input_state)
            task.status = "success"
            task.result = final_state
            task.finished_at = time.monotonic()
            log.info(
                "meta_graph.task_success",
                task_id=task.task_id,
                duration_s=round(
                    task.finished_at - (task.started_at or task.finished_at), 4
                ),
            )
        except Exception as exc:
            task.status = "failed"
            task.error = str(exc)
            task.finished_at = time.monotonic()
            log.error(
                "meta_graph.task_failed",
                task_id=task.task_id,
                error=task.error,
                duration_s=round(
                    task.finished_at - (task.started_at or task.finished_at), 4
                ),
            )


# ---------------------------------------------------------------------------
# Convenience wrapper
# ---------------------------------------------------------------------------


async def run_meta_graph(
    tasks: list[SubAgentTask],
    run_graph: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
    *,
    max_parallel: int = 4,
    fail_fast: bool = True,
) -> MetaRunResult:
    """Top-level convenience function.

    Instantiates a :class:`MetaGraphScheduler` and runs it to completion.

    Args:
        tasks: All tasks to schedule.
        run_graph: Async callable (initial-state dict → final-state dict).
        max_parallel: Maximum concurrent sub-agent invocations.
        fail_fast: Abort remaining tasks on first failure when *True*.

    Returns:
        A :class:`MetaRunResult` with full per-task detail and aggregate counts.
    """
    scheduler = MetaGraphScheduler(
        tasks,
        run_graph,
        max_parallel=max_parallel,
        fail_fast=fail_fast,
    )
    return await scheduler.run()
