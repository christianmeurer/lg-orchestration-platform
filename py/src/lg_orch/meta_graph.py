"""Dependency-aware multi-agent scheduler (Gap 3, Wave 8).

This module is intentionally decoupled from graph.py and state.py.
Callers supply a ``run_graph`` callable that wraps the actual LangGraph
invocation; this module only handles DAG construction, scheduling,
concurrency control, and result collection.

Exported public names:
    SubAgentTask, DependencyGraph, DependencyPatch, MetaGraphScheduler,
    MetaRunResult, run_meta_graph
"""
from __future__ import annotations

import asyncio
import dataclasses
import time
from collections import deque
from typing import Any, Awaitable, Callable, Literal

import structlog

from lg_orch.worktree import WorktreeLease

__all__ = [
    "SubAgentTask",
    "DependencyGraph",
    "DependencyPatch",
    "MetaGraphScheduler",
    "MetaRunResult",
    "run_meta_graph",
]

# Sentinel used when no worktree base path is provided but isolation is on.
_WORKTREE_BASE_ENV_KEY = "LG_ORCH_REPO_BASE"

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
class DependencyPatch:
    """Describes runtime additions and removals of edges in the dependency graph.

    A sub-agent may include a ``DependencyPatch`` under the
    ``"dependency_patch"`` key of its result dict.  When the scheduler has
    ``dynamic_rewiring=True``, it applies the patch before re-evaluating the
    ready queue.

    Fields:
        add_edges:    List of ``(from_task_id, to_task_id)`` pairs to add.
                      Each addition is cycle-checked; a cyclic addition causes
                      the entire patch to be discarded.
        remove_edges: List of ``(from_task_id, to_task_id)`` pairs to remove.
                      Pairs that are not present in the graph are silently
                      ignored (no-op).
    """

    add_edges: list[tuple[str, str]] = dataclasses.field(default_factory=list)
    remove_edges: list[tuple[str, str]] = dataclasses.field(default_factory=list)


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

    The graph maintains its own internal ``_depends_on`` mapping so that
    :meth:`add_edge`, :meth:`remove_edge`, and :meth:`clone` can mutate edge
    structures without touching the original :class:`SubAgentTask` objects.
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
        # Internal adjacency: maps each task_id to the list of task_ids it
        # depends on.  This is a graph-owned copy so mutations here do not
        # propagate back to SubAgentTask.depends_on.
        self._depends_on: dict[str, list[str]] = {
            t.task_id: list(t.depends_on) for t in tasks
        }
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
        for task_id, deps in self._depends_on.items():
            for dep in deps:
                if dep not in self._id_set:
                    raise ValueError(
                        f"Task '{task_id}' declares unknown dependency '{dep}'"
                    )

        # Build in-degree map and reverse-adjacency map.
        in_degree: dict[str, int] = {tid: 0 for tid in self._id_set}
        dependents: dict[str, list[str]] = {tid: [] for tid in self._id_set}
        for task_id, deps in self._depends_on.items():
            for dep in deps:
                in_degree[task_id] += 1
                dependents[dep].append(task_id)

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
    # Mutation methods
    # ------------------------------------------------------------------

    def add_edge(self, from_id: str, to_id: str) -> None:
        """Add a directed edge ``from_id → to_id`` (``to_id`` depends on ``from_id``).

        Both ``from_id`` and ``to_id`` must already be nodes in the graph.
        Raises ``ValueError`` if either node is unknown or if adding the edge
        would introduce a cycle.
        """
        if from_id not in self._id_set:
            raise ValueError(
                f"add_edge: unknown task_id '{from_id}'"
            )
        if to_id not in self._id_set:
            raise ValueError(
                f"add_edge: unknown task_id '{to_id}'"
            )
        if from_id in self._depends_on[to_id]:
            # Edge already exists; idempotent.
            return
        self._depends_on[to_id].append(from_id)
        try:
            self._validate_no_cycles()
        except ValueError:
            # Roll back the addition before re-raising.
            self._depends_on[to_id].remove(from_id)
            raise

    def remove_edge(self, from_id: str, to_id: str) -> None:
        """Remove the directed edge ``from_id → to_id`` if it exists.

        Silently does nothing if the edge is not present.
        """
        deps = self._depends_on.get(to_id)
        if deps is not None and from_id in deps:
            deps.remove(from_id)

    def clone(self) -> DependencyGraph:
        """Return a deep copy of this graph's adjacency structures.

        The returned graph shares the same :class:`SubAgentTask` object
        references (task statuses are shared) but owns an independent copy
        of the edge map so mutations on the clone do not affect the original.
        """
        new_graph: DependencyGraph = object.__new__(DependencyGraph)
        new_graph._tasks = self._tasks  # shared task objects intentionally
        new_graph._id_set = set(self._id_set)
        new_graph._depends_on = {
            tid: list(deps) for tid, deps in self._depends_on.items()
        }
        return new_graph

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
            if all(dep in completed_ids for dep in self._depends_on[task.task_id]):
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
            if not any(
                dep in failed_ids for dep in self._depends_on[task.task_id]
            ):
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
        dynamic_rewiring: When *True*, inspect completed task results for a
                          ``"dependency_patch"`` key containing a
                          :class:`DependencyPatch` and apply it to the live
                          graph before the next scheduling cycle.  When
                          *False* (default), any such patch is silently
                          ignored and existing behaviour is preserved.
    """

    def __init__(
        self,
        tasks: list[SubAgentTask],
        run_graph: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
        *,
        max_parallel: int = 4,
        fail_fast: bool = True,
        worktree_isolation: bool = False,
        worktree_base_path: str = ".",
        dynamic_rewiring: bool = False,
    ) -> None:
        self.tasks = tasks
        # run_graph may legitimately be None in test setups that only test
        # isolation wiring; callers that actually invoke run() must provide it.
        self._run_graph: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] = (
            run_graph
            if run_graph is not None
            else _missing_run_graph
        )
        self._max_parallel = max(1, max_parallel)
        self._fail_fast = fail_fast
        self._worktree_isolation = worktree_isolation
        self._worktree_base_path = worktree_base_path
        self._dynamic_rewiring = dynamic_rewiring
        # Populated by run() so that _run_task_plain / _run_task_isolated can
        # apply dependency patches to the live graph.  None outside of run().
        self._dag: DependencyGraph | None = None

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
        self._dag = dag
        sema = asyncio.Semaphore(self._max_parallel)
        completed_ids: set[str] = set()
        failed_ids: set[str] = set()
        started_ids: set[str] = set()
        # Maps the live asyncio.Task → the SubAgentTask it is executing.
        running: dict[asyncio.Task[None], SubAgentTask] = {}
        start = time.monotonic()

        try:
            while True:
                # ── Launch all newly-ready tasks ──────────────────────────────
                for task in self._dag.ready_tasks(completed_ids, failed_ids):
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
        finally:
            self._dag = None

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
        """Invoke ``run_graph`` for a single task and record the outcome.

        When ``worktree_isolation=True`` a :class:`WorktreeLease` is acquired
        before calling ``run_graph`` and the ``worktree_path`` is injected into
        the input state as ``state["worktree_path"]``.  The lease is released
        (with a merge attempt on success, removal-only on failure) after the
        call returns.
        """
        task.status = "running"
        task.started_at = time.monotonic()
        log.info("meta_graph.task_start", task_id=task.task_id)

        if self._worktree_isolation:
            await self._run_task_isolated(task)
        else:
            await self._run_task_plain(task)

    async def _run_task_plain(self, task: SubAgentTask) -> None:
        """Execute the task without worktree isolation."""
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
            self._maybe_apply_patch(task.task_id, final_state)
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

    async def _run_task_isolated(self, task: SubAgentTask) -> None:
        """Execute the task inside a :class:`WorktreeLease`."""
        succeeded = False
        final_state: dict[str, Any] = {}
        try:
            async with WorktreeLease(
                task.task_id,
                self._worktree_base_path,
                merge=True,
            ) as wt_ctx:
                state_with_path: dict[str, Any] = {
                    **task.input_state,
                    "worktree_path": wt_ctx.worktree_path,
                }
                final_state = await self._run_graph(state_with_path)
                succeeded = True
            task.status = "success"
            task.result = final_state
            task.finished_at = time.monotonic()
            log.info(
                "meta_graph.task_success",
                task_id=task.task_id,
                worktree_path=final_state.get("worktree_path"),
                duration_s=round(
                    task.finished_at - (task.started_at or task.finished_at), 4
                ),
            )
            self._maybe_apply_patch(task.task_id, final_state)
        except Exception as exc:
            if not succeeded:
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

    # ------------------------------------------------------------------
    # Dynamic dependency patching
    # ------------------------------------------------------------------

    def _maybe_apply_patch(
        self, task_id: str, result: dict[str, Any]
    ) -> None:
        """Apply a :class:`DependencyPatch` from *result* if rewiring is enabled.

        This method is synchronous (no awaits) so it is effectively atomic
        from the asyncio event loop's perspective.  The live graph is only
        replaced when the candidate patched clone passes the cycle check.
        """
        if not self._dynamic_rewiring:
            return
        if self._dag is None:
            return

        raw_patch = result.get("dependency_patch")
        if raw_patch is None:
            return
        if not isinstance(raw_patch, DependencyPatch):
            log.warning(
                "meta_graph.patch_invalid_type",
                task_id=task_id,
                patch_type=type(raw_patch).__name__,
            )
            return

        candidate = self._dag.clone()
        try:
            for from_id, to_id in raw_patch.remove_edges:
                candidate.remove_edge(from_id, to_id)
            for from_id, to_id in raw_patch.add_edges:
                candidate.add_edge(from_id, to_id)
        except ValueError as exc:
            log.warning(
                "meta_graph.patch_discarded",
                task_id=task_id,
                reason=str(exc),
            )
            return

        # Atomic swap: replace the live graph with the validated clone.
        self._dag = candidate
        log.info(
            "meta_graph.patch_applied",
            task_id=task_id,
            added=raw_patch.add_edges,
            removed=raw_patch.remove_edges,
        )


# ---------------------------------------------------------------------------
# Convenience wrapper
# ---------------------------------------------------------------------------


def _missing_run_graph(state: dict[str, Any]) -> Any:
    raise RuntimeError(
        "MetaGraphScheduler.run() called but no run_graph was provided."
    )


async def run_meta_graph(
    tasks: list[SubAgentTask],
    run_graph: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
    *,
    max_parallel: int = 4,
    fail_fast: bool = True,
    worktree_isolation: bool = False,
    worktree_base_path: str = ".",
    dynamic_rewiring: bool = False,
) -> MetaRunResult:
    """Top-level convenience function.

    Instantiates a :class:`MetaGraphScheduler` and runs it to completion.

    Args:
        tasks: All tasks to schedule.
        run_graph: Async callable (initial-state dict → final-state dict).
        max_parallel: Maximum concurrent sub-agent invocations.
        fail_fast: Abort remaining tasks on first failure when *True*.
        worktree_isolation: When *True*, each task runs inside a git worktree.
        worktree_base_path: Root of the git repo used for worktree creation.
        dynamic_rewiring: When *True*, honour ``"dependency_patch"`` keys in
                          task results.  When *False* (default), patches are
                          ignored and behaviour is identical to pre-Wave-8.

    Returns:
        A :class:`MetaRunResult` with full per-task detail and aggregate counts.
    """
    scheduler = MetaGraphScheduler(
        tasks,
        run_graph,
        max_parallel=max_parallel,
        fail_fast=fail_fast,
        worktree_isolation=worktree_isolation,
        worktree_base_path=worktree_base_path,
        dynamic_rewiring=dynamic_rewiring,
    )
    return await scheduler.run()
