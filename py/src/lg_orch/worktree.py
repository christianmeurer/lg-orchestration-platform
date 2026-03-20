# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Christian Meurer — https://github.com/christianmeurer/Lula
"""Git-worktree-based branch isolation for parallel agents (Wave 8).

Public surface:
    WorktreeContext, WorktreeError, WorktreeLease,
    create_worktree, remove_worktree, merge_worktree
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
from types import TracebackType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

__all__ = [
    "WorktreeContext",
    "WorktreeError",
    "WorktreeLease",
    "create_worktree",
    "remove_worktree",
    "merge_worktree",
]

log = logging.getLogger(__name__)


class WorktreeError(Exception):
    """Raised when a git worktree operation fails."""


@dataclasses.dataclass
class WorktreeContext:
    """Describes a live git worktree created for one agent run."""

    run_id: str
    branch: str        # "lg-orch/{run_id}"
    worktree_path: str  # absolute path to the worktree directory
    base_branch: str   # e.g. "main" or the HEAD branch at creation time


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
    log.debug(
        "git %s → rc=%d stdout=%r stderr=%r",
        " ".join(args),
        rc,
        stdout,
        stderr,
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
    rc, current_branch, err = await _run_git(
        "rev-parse", "--abbrev-ref", "HEAD", cwd=base_path
    )
    if rc != 0:
        raise WorktreeError(
            f"git rev-parse --abbrev-ref HEAD failed (rc={rc}): {err}"
        )
    base_branch = current_branch or "main"

    # Create the worktree with a new branch.
    rc, out, err = await _run_git(
        "worktree", "add", "-b", branch, worktree_path,
        cwd=base_path,
    )
    if rc != 0:
        raise WorktreeError(
            f"git worktree add failed for run_id={run_id!r} (rc={rc}): {err}"
        )

    log.debug("worktree created: branch=%r path=%r", branch, worktree_path)
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
    rc, _, err = await _run_git(
        "worktree", "remove", "--force", ctx.worktree_path,
    )
    if rc != 0:
        log.warning(
            "git worktree remove failed for %r (rc=%d): %s",
            ctx.worktree_path,
            rc,
            err,
        )

    rc, _, err = await _run_git("branch", "-D", ctx.branch)
    if rc != 0:
        log.warning(
            "git branch -D failed for %r (rc=%d): %s",
            ctx.branch,
            rc,
            err,
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
    rc, _, err = await _run_git("checkout", ctx.base_branch)
    if rc != 0:
        raise WorktreeError(
            f"git checkout {ctx.base_branch!r} failed (rc={rc}): {err}"
        )

    rc, _, err = await _run_git(
        "merge",
        "--no-ff",
        f"--strategy={strategy}",
        ctx.branch,
    )
    if rc != 0:
        raise WorktreeError(
            f"git merge of {ctx.branch!r} into {ctx.base_branch!r} failed "
            f"(rc={rc}): {err}"
        )

    log.debug(
        "worktree branch %r merged into %r with strategy=%r",
        ctx.branch,
        ctx.base_branch,
        strategy,
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
            except WorktreeError as exc:
                log.warning("worktree merge failed for %r: %s", self._run_id, exc)
        await remove_worktree(self._ctx)
