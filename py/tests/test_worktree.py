"""Tests for py/src/lg_orch/worktree.py (Wave 8).

All git subprocess calls are mocked via unittest.mock.patch so tests run
without a real git repository.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lg_orch.worktree import (
    WorktreeContext,
    WorktreeError,
    WorktreeLease,
    cleanup_orphaned_worktrees,
    create_worktree,
    merge_worktree,
    remove_worktree,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_proc(returncode: int, stdout: str = "", stderr: str = "") -> MagicMock:
    """Return a mock asyncio subprocess with the given outcome."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout.encode(), stderr.encode()))
    return proc


def _make_ctx(
    run_id: str = "test-run",
    branch: str = "lg-orch/test-run",
    worktree_path: str = "/repo/.lg_orch_worktrees/test-run",
    base_branch: str = "main",
) -> WorktreeContext:
    return WorktreeContext(
        run_id=run_id,
        branch=branch,
        worktree_path=worktree_path,
        base_branch=base_branch,
    )


# ---------------------------------------------------------------------------
# create_worktree
# ---------------------------------------------------------------------------


class TestCreateWorktree:
    def test_create_worktree_creates_correct_branch_name(self) -> None:
        """Branch must be f'lg-orch/{run_id}'."""
        run_id = "abc-123"

        # Call sequence: rev-parse (rc=0), worktree add (rc=0)
        rev_parse_proc = _make_proc(0, stdout="main")
        add_proc = _make_proc(0)

        with patch(
            "lg_orch.worktree.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            side_effect=[rev_parse_proc, add_proc],
        ):
            ctx = asyncio.run(create_worktree(run_id, "/repo"))

        assert ctx.branch == f"lg-orch/{run_id}"
        assert ctx.run_id == run_id
        assert ctx.base_branch == "main"
        assert run_id in ctx.worktree_path

    def test_create_worktree_raises_on_git_error(self) -> None:
        """WorktreeError must be raised when git worktree add exits non-zero."""
        run_id = "fail-run"

        rev_parse_proc = _make_proc(0, stdout="main")
        add_proc = _make_proc(1, stderr="fatal: could not create worktree")

        with (
            patch(
                "lg_orch.worktree.asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                side_effect=[rev_parse_proc, add_proc],
            ),
            pytest.raises(WorktreeError, match="git worktree add failed"),
        ):
            asyncio.run(create_worktree(run_id, "/repo"))

    def test_create_worktree_raises_when_rev_parse_fails(self) -> None:
        """WorktreeError raised when rev-parse itself fails."""
        rev_parse_proc = _make_proc(1, stderr="not a git repo")

        with (
            patch(
                "lg_orch.worktree.asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=rev_parse_proc,
            ),
            pytest.raises(WorktreeError, match="git rev-parse"),
        ):
            asyncio.run(create_worktree("x", "/no-repo"))


# ---------------------------------------------------------------------------
# remove_worktree
# ---------------------------------------------------------------------------


class TestRemoveWorktree:
    def test_remove_worktree_does_not_raise_on_failure(self) -> None:
        """remove_worktree must not raise even if git commands fail."""
        ctx = _make_ctx()
        bad_proc = _make_proc(1, stderr="error")

        with patch(
            "lg_orch.worktree.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=bad_proc,
        ):
            # Must not raise
            asyncio.run(remove_worktree(ctx))

    def test_remove_worktree_success(self) -> None:
        """remove_worktree completes silently when git commands succeed."""
        ctx = _make_ctx()
        ok_proc = _make_proc(0)

        with patch(
            "lg_orch.worktree.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=ok_proc,
        ):
            asyncio.run(remove_worktree(ctx))  # no assertion needed — must not raise


# ---------------------------------------------------------------------------
# merge_worktree
# ---------------------------------------------------------------------------


class TestMergeWorktree:
    def test_merge_worktree_success(self) -> None:
        """merge_worktree completes without raising when both git calls succeed."""
        ctx = _make_ctx()
        ok_proc = _make_proc(0)

        with patch(
            "lg_orch.worktree.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=ok_proc,
        ):
            asyncio.run(merge_worktree(ctx))

    def test_merge_worktree_raises_on_conflict(self) -> None:
        """WorktreeError raised when git merge exits non-zero."""
        ctx = _make_ctx()
        checkout_proc = _make_proc(0)
        merge_proc = _make_proc(1, stderr="CONFLICT (content)")

        with (
            patch(
                "lg_orch.worktree.asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                side_effect=[checkout_proc, merge_proc],
            ),
            pytest.raises(WorktreeError, match="git merge"),
        ):
            asyncio.run(merge_worktree(ctx))

    def test_merge_worktree_raises_on_checkout_failure(self) -> None:
        """WorktreeError raised when git checkout fails before the merge."""
        ctx = _make_ctx()
        bad_checkout = _make_proc(1, stderr="pathspec error")

        with (
            patch(
                "lg_orch.worktree.asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=bad_checkout,
            ),
            pytest.raises(WorktreeError, match="git checkout"),
        ):
            asyncio.run(merge_worktree(ctx))


# ---------------------------------------------------------------------------
# WorktreeLease
# ---------------------------------------------------------------------------


class TestWorktreeLease:
    """Tests for WorktreeLease async context manager."""

    # Shared mock factory — patches create_worktree / remove_worktree /
    # merge_worktree at the module level so the context manager uses them.

    def _patch_all(
        self,
        *,
        create_ok: bool = True,
        remove_ok: bool = True,
        merge_ok: bool = True,
    ) -> tuple[AsyncMock, AsyncMock, AsyncMock]:
        ctx = _make_ctx()

        create_mock = AsyncMock(return_value=ctx)
        remove_mock = AsyncMock()

        if merge_ok:
            merge_mock = AsyncMock()
        else:
            merge_mock = AsyncMock(side_effect=WorktreeError("merge conflict"))

        return create_mock, remove_mock, merge_mock

    def test_worktree_lease_calls_remove_on_exit(self) -> None:
        """remove_worktree must be called on clean exit."""
        create_mock, remove_mock, merge_mock = self._patch_all()

        async def _run() -> None:
            with (
                patch("lg_orch.worktree.create_worktree", create_mock),
                patch("lg_orch.worktree.remove_worktree", remove_mock),
                patch("lg_orch.worktree.merge_worktree", merge_mock),
            ):
                async with WorktreeLease("r1", "/repo"):
                    pass

        asyncio.run(_run())
        remove_mock.assert_awaited_once()

    def test_worktree_lease_merges_on_clean_exit(self) -> None:
        """merge_worktree must be called on clean exit (no exception)."""
        create_mock, remove_mock, merge_mock = self._patch_all()

        async def _run() -> None:
            with (
                patch("lg_orch.worktree.create_worktree", create_mock),
                patch("lg_orch.worktree.remove_worktree", remove_mock),
                patch("lg_orch.worktree.merge_worktree", merge_mock),
            ):
                async with WorktreeLease("r2", "/repo", merge=True):
                    pass

        asyncio.run(_run())
        merge_mock.assert_awaited_once()
        remove_mock.assert_awaited_once()

    def test_worktree_lease_skips_merge_on_exception(self) -> None:
        """On exception in body: merge must NOT be called; remove must be called."""
        create_mock, remove_mock, merge_mock = self._patch_all()

        async def _run() -> None:
            with (
                patch("lg_orch.worktree.create_worktree", create_mock),
                patch("lg_orch.worktree.remove_worktree", remove_mock),
                patch("lg_orch.worktree.merge_worktree", merge_mock),
                pytest.raises(RuntimeError, match="body error"),
            ):
                async with WorktreeLease("r3", "/repo", merge=True):
                    raise RuntimeError("body error")

        asyncio.run(_run())
        merge_mock.assert_not_awaited()
        remove_mock.assert_awaited_once()

    def test_worktree_lease_merge_false_skips_merge_on_clean_exit(self) -> None:
        """When merge=False, merge_worktree must not be called even on clean exit."""
        create_mock, remove_mock, merge_mock = self._patch_all()

        async def _run() -> None:
            with (
                patch("lg_orch.worktree.create_worktree", create_mock),
                patch("lg_orch.worktree.remove_worktree", remove_mock),
                patch("lg_orch.worktree.merge_worktree", merge_mock),
            ):
                async with WorktreeLease("r4", "/repo", merge=False):
                    pass

        asyncio.run(_run())
        merge_mock.assert_not_awaited()
        remove_mock.assert_awaited_once()

    def test_worktree_lease_aenter_returns_context(self) -> None:
        """__aenter__ must return the WorktreeContext produced by create_worktree."""
        expected_ctx = _make_ctx(run_id="ret-test")
        create_mock = AsyncMock(return_value=expected_ctx)
        remove_mock = AsyncMock()
        merge_mock = AsyncMock()

        received: list[WorktreeContext] = []

        async def _run() -> None:
            with (
                patch("lg_orch.worktree.create_worktree", create_mock),
                patch("lg_orch.worktree.remove_worktree", remove_mock),
                patch("lg_orch.worktree.merge_worktree", merge_mock),
            ):
                async with WorktreeLease("ret-test", "/repo") as ctx:
                    received.append(ctx)

        asyncio.run(_run())
        assert len(received) == 1
        assert received[0] is expected_ctx


# ---------------------------------------------------------------------------
# cleanup_orphaned_worktrees
# ---------------------------------------------------------------------------


class TestCleanupOrphanedWorktrees:
    def test_cleanup_orphaned_worktrees_returns_list(self, tmp_path: pytest.TempPathFactory) -> None:
        """cleanup_orphaned_worktrees returns a list (may be empty in test env)."""
        import subprocess as sp

        sp.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
        sp.run(
            ["git", "commit", "--allow-empty", "-m", "init"],
            cwd=str(tmp_path),
            capture_output=True,
        )
        result = cleanup_orphaned_worktrees(tmp_path)
        assert isinstance(result, list)

    def test_cleanup_orphaned_worktrees_nonexistent_path(
        self, tmp_path: pytest.TempPathFactory
    ) -> None:
        """cleanup_orphaned_worktrees handles non-git directories gracefully."""
        result = cleanup_orphaned_worktrees(tmp_path)
        assert isinstance(result, list)
        assert result == []

    def test_cleanup_orphaned_worktrees_removes_orphan(
        self, tmp_path: pytest.TempPathFactory
    ) -> None:
        """cleanup_orphaned_worktrees removes worktrees whose path no longer exists."""
        import subprocess as sp

        # Simulate porcelain output with an orphaned lg-orch worktree
        porcelain_output = (
            "worktree /repo\n"
            "HEAD abc123\n"
            "branch refs/heads/main\n"
            "\n"
            "worktree /nonexistent/path\n"
            "HEAD def456\n"
            "branch refs/heads/lg-orch/orphan-run\n"
            "\n"
        )

        with patch(
            "lg_orch.worktree.subprocess.run",
        ) as mock_run:
            # First call: git worktree list --porcelain
            list_result = MagicMock()
            list_result.returncode = 0
            list_result.stdout = porcelain_output

            # Subsequent calls: git worktree remove, git branch -D
            remove_result = MagicMock()
            remove_result.returncode = 0

            mock_run.side_effect = [list_result, remove_result, remove_result]

            result = cleanup_orphaned_worktrees("/repo")

        assert result == ["/nonexistent/path"]
        assert mock_run.call_count == 3
