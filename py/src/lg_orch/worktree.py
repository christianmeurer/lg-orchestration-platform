# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Christian Meurer — https://github.com/christianmeurer/Lula
"""Git-worktree-based branch isolation for parallel agents (Wave 8).

Public surface:
    WorktreeContext, WorktreeError, WorktreeLease,
    create_worktree, remove_worktree, merge_worktree,
    cleanup_orphaned_worktrees
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import subprocess
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    pass

__all__ = [
    "WorktreeContext",
    "WorktreeError",
    "WorktreeLease",
    "cleanup_orphaned_worktrees",
    "create_worktree",
    "merge_worktree",
    "remove_worktree",
]

_log = structlog.get_logger(__name__)


class WorktreeError(Exception):
    """Raised when a git worktree operation fails."""


@dataclasses.dataclass
class WorktreeContext:
    """Describes a live git worktree created for one agent run."""

    run_id: str
    branch: str  # "lg-orch/{run_id}"
    worktree_path: str  # absolute path to the worktree directory
    base_branch: str  # e.g. "main" or the HEAD branch at creation time


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _run_git(*args: str, cwd: str | None = None) -> tuple[int, str, str]:
    """Run a git command via asyncio subprocess.

    Returns:
        (returncode, stdout_text, stderr_text)

    Never raises; callers decide what to do with a non-zero returncode.
    """
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    raw_out, raw_err = await proc.communicate()
    stdout = raw_out.decode(errors="replace").strip()
    stderr = raw_err.decode(errors="replace").strip()
    rc: int = proc.returncode if proc.returncode is not None else 1
    _log.debug(
        "worktree.git_cmd",
        args=" ".join(args),
        rc=rc,
        stdout=stdout,
        stderr=stderr,
    )
    return rc, stdout, stderr


# ---------------------------------------------------------------------------
# Public async functions
# ---------------------------------------------------------------------------


async def create_worktree(run_id: str, base_path: str) -> WorktreeContext:
    """Create a git worktree for *run_id* under *base_path*.

    The worktree is placed at ``<base_path>/.lg_orch_worktrees/<run_id>``.
    A new branch ``lg-orch/<run_id>`` is created from the current HEAD.

    Args:
        run_id:    Unique identifier for the agent run (used as branch suffix).
        base_path: Root of the git repository checkout; the worktree
                   directory is created inside it.

    Returns:
        A :class:`WorktreeContext` describing the newly created worktree.

    Raises:
        WorktreeError: If any git command exits with a non-zero return code.
    """
    branch = f"lg-orch/{run_id}"
    worktrees_root = os.path.join(base_path, ".lg_orch_worktrees")
    worktree_path = os.path.join(worktrees_root, run_id)

    # Discover the current branch so we know where to merge back later.
    rc, current_branch, err = await _run_git("rev-parse", "--abbrev-ref", "HEAD", cwd=base_path)
    if rc != 0:
        raise WorktreeError(f"git rev-parse --abbrev-ref HEAD failed (rc={rc}): {err}")
    base_branch = current_branch or "main"

    # Create the worktree with a new branch.
    rc, _out, err = await _run_git(
        "worktree",
        "add",
        "-b",
        branch,
        worktree_path,
        cwd=base_path,
    )
    if rc != 0:
        raise WorktreeError(f"git worktree add failed for run_id={run_id!r} (rc={rc}): {err}")

    _log.debug("worktree.created", branch=branch, path=str(worktree_path))
    return WorktreeContext(
        run_id=run_id,
        branch=branch,
        worktree_path=os.path.abspath(worktree_path),
        base_branch=base_branch,
    )


async def remove_worktree(ctx: WorktreeContext) -> None:
    """Remove the worktree and its branch described by *ctx*.

    Runs ``git worktree remove --force`` followed by ``git branch -D``.
    Logs a warning on any failure but does **not** raise.

    Args:
        ctx: The :class:`WorktreeContext` to clean up.
    """
    # MEDIUM FIX 4: Pass cwd to _run_git so operations target the correct repo.
    # Derive base_path from worktree_path (parent of .lg_orch_worktrees/<run_id>).
    _base_path = str(Path(ctx.worktree_path).parent.parent)
    rc, _, err = await _run_git(
        "worktree",
        "remove",
        "--force",
        ctx.worktree_path,
        cwd=_base_path,
    )
    if rc != 0:
        _log.warning(
            "worktree.remove_failed",
            path=ctx.worktree_path,
            rc=rc,
            stderr=err,
        )

    rc, _, err = await _run_git("branch", "-D", ctx.branch, cwd=_base_path)
    if rc != 0:
        _log.warning(
            "worktree.branch_delete_failed",
            branch=ctx.branch,
            rc=rc,
            stderr=err,
        )


async def merge_worktree(ctx: WorktreeContext, strategy: str = "ours") -> None:
    """Merge the worktree branch back into the base branch.

    Checks out *ctx.base_branch*, then runs
    ``git merge --no-ff --strategy=<strategy> <ctx.branch>``.

    Args:
        ctx:      The :class:`WorktreeContext` whose branch should be merged.
        strategy: Merge strategy passed to ``--strategy``.  Defaults to
                  ``"ours"`` (keeps base-branch content on conflict).

    Raises:
        WorktreeError: If either git command exits with a non-zero code
                       (e.g. a merge conflict that cannot be auto-resolved).
    """
    # MEDIUM FIX 4: Pass cwd to _run_git so checkout/merge target the correct repo.
    _base_path = str(Path(ctx.worktree_path).parent.parent)
    rc, _, err = await _run_git("checkout", ctx.base_branch, cwd=_base_path)
    if rc != 0:
        raise WorktreeError(f"git checkout {ctx.base_branch!r} failed (rc={rc}): {err}")

    rc, _, err = await _run_git(
        "merge",
        "--no-ff",
        f"--strategy={strategy}",
        ctx.branch,
        cwd=_base_path,
    )
    if rc != 0:
        raise WorktreeError(
            f"git merge of {ctx.branch!r} into {ctx.base_branch!r} failed (rc={rc}): {err}"
        )

    _log.debug(
        "worktree.merged",
        branch=ctx.branch,
        base_branch=ctx.base_branch,
        strategy=strategy,
    )


# ---------------------------------------------------------------------------
# Context manager
# ---------------------------------------------------------------------------


class WorktreeLease:
    """Async context manager providing a temporary git worktree.

    On successful exit (no exception raised inside the ``async with`` block)
    the worktree branch is merged back into the base branch before the
    worktree is removed.  On exceptional exit the merge is skipped; the
    worktree is still removed.

    Args:
        run_id:    Unique identifier for the agent run.
        base_path: Root of the git repository checkout.
        merge:     If *False*, skip the merge-back step even on clean exit.
    """

    def __init__(
        self,
        run_id: str,
        base_path: str,
        merge: bool = True,
    ) -> None:
        self._run_id = run_id
        self._base_path = base_path
        self._merge = merge
        self._ctx: WorktreeContext | None = None

    async def __aenter__(self) -> WorktreeContext:
        self._ctx = await create_worktree(self._run_id, self._base_path)
        return self._ctx

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if self._ctx is None:
            return
        if exc_type is None and self._merge:
            # Clean exit — attempt merge; if it fails, log and continue to
            # worktree removal so we do not leave stale directories behind.
            try:
                await merge_worktree(self._ctx)
            except WorktreeError:
                _log.warning("worktree.merge_failed", run_id=self._run_id, exc_info=True)
        await remove_worktree(self._ctx)


# ---------------------------------------------------------------------------
# Orphan recovery (startup cleanup)
# ---------------------------------------------------------------------------


def cleanup_orphaned_worktrees(base_path: str | Path) -> list[str]:
    """Scan for and remove orphaned lg-orch worktrees.

    Orphaned worktrees are created when a pod restarts mid-task, leaving
    git worktrees on disk with no corresponding WorktreeLease in memory.

    Runs ``git worktree list --porcelain`` and removes any worktree whose
    branch name starts with ``lg-orch/`` and whose path no longer exists
    on disk.

    Returns a list of removed worktree paths.
    """
    base_path = Path(base_path)
    removed: list[str] = []

    try:
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=str(base_path),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return removed

        # Parse porcelain output: blocks separated by blank lines
        # Each block: "worktree <path>\nHEAD <sha>\nbranch refs/heads/<name>\n"
        current_path: str | None = None
        current_branch: str | None = None

        for line in result.stdout.splitlines():
            if line.startswith("worktree "):
                current_path = line[len("worktree "):].strip()
                current_branch = None
            elif line.startswith("branch "):
                current_branch = line[len("branch "):].strip()
            elif line == "" and current_path and current_branch:
                # End of block — check if this is an lg-orch worktree
                branch_short = current_branch.removeprefix("refs/heads/")
                if branch_short.startswith("lg-orch/") and not Path(current_path).exists():
                    try:
                        subprocess.run(
                            ["git", "worktree", "remove", "--force", current_path],
                            cwd=str(base_path),
                            capture_output=True,
                            timeout=30,
                        )
                        # Also delete the branch
                        subprocess.run(
                            ["git", "branch", "-D", branch_short],
                            cwd=str(base_path),
                            capture_output=True,
                            timeout=30,
                        )
                        removed.append(current_path)
                    except Exception as e:
                        logging.warning(
                            "Failed to remove orphaned worktree %s: %s",
                            current_path,
                            e,
                        )
                current_path = None
                current_branch = None

    except Exception as e:
        logging.warning("cleanup_orphaned_worktrees failed: %s", e)

    return removed
