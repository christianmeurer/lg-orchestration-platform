# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Christian Meurer — https://github.com/christianmeurer/Lula
"""Multi-repository orchestration (Wave 9 – Cross-Repository Microservice Orchestration).

Extends :class:`~lg_orch.meta_graph.MetaGraphScheduler` to fan out sub-agents
across multiple repository roots, injecting repo-specific context and typed
cross-repo handoffs into each task's initial state before execution.

Exported public names:
    RepoConfig, CrossRepoHandoff, MultiRepoScheduler
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any

import structlog

from lg_orch.meta_graph import (
    MetaGraphScheduler,
    MetaRunResult,
    SubAgentTask,
)
from lg_orch.scip_index import ScipIndex, ScipSymbol, load_scip_index

__all__ = [
    "RepoConfig",
    "CrossRepoHandoff",
    "MultiRepoScheduler",
]

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_SCIP_SUMMARY_LIMIT = 20  # top N symbols injected into task state


@dataclass
class RepoConfig:
    """Configuration for a single repository in a multi-repo orchestration."""

    name: str           # logical name, e.g. "auth-service"
    root_path: str      # absolute path to the repo
    runner_url: str     # URL of the Rust runner serving this repo
    scip_index: ScipIndex | None = None


@dataclass
class CrossRepoHandoff:
    """Typed handoff between agents working on different repos."""

    source_repo: str
    target_repo: str
    shared_symbols: list[str]    # symbol names that cross the boundary
    objective: str
    context_patch: dict[str, Any] = field(default_factory=dict)


def _scip_summary(index: ScipIndex | None) -> list[dict[str, Any]]:
    """Return a truncated, serialisable summary of the top symbols in *index*."""
    if index is None:
        return []
    sorted_syms: list[ScipSymbol] = sorted(index.symbols, key=lambda s: s.name)
    top: list[ScipSymbol] = sorted_syms[:_SCIP_SUMMARY_LIMIT]
    return [
        {
            "name": sym.name,
            "kind": sym.kind,
            "file_path": sym.file_path,
            "start_line": sym.start_line,
            "end_line": sym.end_line,
        }
        for sym in top
    ]


class MultiRepoScheduler:
    """Wraps :class:`~lg_orch.meta_graph.MetaGraphScheduler`.

    Each :class:`~lg_orch.meta_graph.SubAgentTask` is associated with a
    :class:`RepoConfig` via *task_repo_map*.  Before running a task, the
    scheduler injects the following keys into ``task.input_state``:

    - ``repo_root``: absolute path to the matched repository.
    - ``runner_url``: URL of the Rust runner for that repository.
    - ``scip_summary``: list of up to 20 symbol dicts from the SCIP index.
    - ``active_handoff``: a ``CrossRepoHandoff`` dict if one targets this task
      (keyed by *task_id*), otherwise the key is absent.

    Args:
        repos: All repository configurations participating in the run.
        task_repo_map: Mapping of ``task_id`` → repo ``name``.
        concurrency: Maximum number of sub-agent tasks running simultaneously.
        worktree_isolation: Forwarded to the inner :class:`MetaGraphScheduler`.
        dynamic_rewiring: Forwarded to the inner :class:`MetaGraphScheduler`.
    """

    def __init__(
        self,
        repos: list[RepoConfig],
        task_repo_map: dict[str, str],
        concurrency: int = 4,
        worktree_isolation: bool = False,
        dynamic_rewiring: bool = False,
    ) -> None:
        self._repos: dict[str, RepoConfig] = {r.name: r for r in repos}
        self._task_repo_map = task_repo_map
        self._concurrency = concurrency
        self._worktree_isolation = worktree_isolation
        self._dynamic_rewiring = dynamic_rewiring

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_repo(self, task_id: str) -> RepoConfig:
        """Return the :class:`RepoConfig` for *task_id*.

        Raises:
            ValueError: If *task_id* is not present in ``task_repo_map``, or if
                the repo name it maps to does not exist in ``repos``.
        """
        if task_id not in self._task_repo_map:
            raise ValueError(
                f"MultiRepoScheduler: task_id '{task_id}' not found in task_repo_map"
            )
        repo_name = self._task_repo_map[task_id]
        if repo_name not in self._repos:
            raise ValueError(
                f"MultiRepoScheduler: repo '{repo_name}' (mapped from task '{task_id}') "
                "not found in repos list"
            )
        return self._repos[repo_name]

    def _enrich_task(
        self,
        task: SubAgentTask,
        handoffs: list[CrossRepoHandoff],
    ) -> None:
        """Mutate ``task.input_state`` in-place with repo context and handoffs."""
        repo = self._resolve_repo(task.task_id)

        # Lazy-load the SCIP index on first encounter if not pre-loaded.
        if repo.scip_index is None:
            repo.scip_index = load_scip_index(repo.root_path)

        task.input_state["repo_root"] = repo.root_path
        task.input_state["runner_url"] = repo.runner_url
        task.input_state["scip_summary"] = _scip_summary(repo.scip_index)

        # Find any handoff whose target_repo matches this task's repo name.
        repo_name = self._task_repo_map[task.task_id]
        matching: list[CrossRepoHandoff] = [
            h for h in handoffs if h.target_repo == repo_name
        ]
        if matching:
            # Use the first matching handoff; convert to a plain dict so the
            # inner graph does not need to import this module's types.
            h = matching[0]
            task.input_state["active_handoff"] = dataclasses.asdict(h)
            log.info(
                "multi_repo.handoff_injected",
                task_id=task.task_id,
                source_repo=h.source_repo,
                target_repo=h.target_repo,
                shared_symbols=h.shared_symbols,
            )

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def run(
        self,
        tasks: list[SubAgentTask],
        handoffs: list[CrossRepoHandoff] | None = None,
    ) -> MetaRunResult:
        """Execute all tasks with repo context injected.

        Steps:
        1. Validate that every task has a known repo mapping (raises ``ValueError``
           immediately if any mapping is missing).
        2. Enrich each task's ``input_state`` with repo context and handoffs.
        3. Delegate to :class:`~lg_orch.meta_graph.MetaGraphScheduler`.

        Args:
            tasks: All sub-agent tasks to schedule.
            handoffs: Optional cross-repo handoffs; injected into matching tasks.

        Returns:
            A :class:`~lg_orch.meta_graph.MetaRunResult` from the inner scheduler.

        Raises:
            ValueError: If any ``task.task_id`` lacks a repo mapping.
        """
        resolved_handoffs: list[CrossRepoHandoff] = handoffs or []

        # Validate all task mappings up-front to fail fast with a clear error.
        for task in tasks:
            self._resolve_repo(task.task_id)

        # Enrich each task in-place.
        for task in tasks:
            self._enrich_task(task, resolved_handoffs)

        scheduler = MetaGraphScheduler(
            tasks,
            run_graph=None,  # caller must ensure tasks carry a run_graph shim
            max_parallel=self._concurrency,
            fail_fast=True,
            worktree_isolation=self._worktree_isolation,
            dynamic_rewiring=self._dynamic_rewiring,
        )
        return await scheduler.run()
