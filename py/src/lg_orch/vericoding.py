# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Christian Meurer — https://github.com/christianmeurer/Lula
"""
Python-side contract wrapper for the Rust runner boundary invariants.

Mirrors the four invariants implemented in rs/runner/src/invariants.rs and
provides a fast pre-check before any tool batch is submitted to the runner,
avoiding a round-trip for violations that are statically detectable.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Exception type
# ---------------------------------------------------------------------------


@dataclass
class InvariantViolation(Exception):
    invariant: str
    message: str

    def __str__(self) -> str:
        return f"invariant_violation[{self.invariant}]: {self.message}"


# ---------------------------------------------------------------------------
# Known tool names (must stay in sync with tools/mod.rs dispatch table)
# ---------------------------------------------------------------------------

_KNOWN_TOOLS: frozenset[str] = frozenset(
    {
        "health",
        "read_file",
        "search_files",
        "search_codebase",
        "ast_index_summary",
        "list_files",
        "apply_patch",
        "exec",
        "undo",
        "mcp_discover",
        "mcp_execute",
        "mcp_resources_list",
        "mcp_resource_read",
        "mcp_prompts_list",
        "mcp_prompt_get",
    }
)

# Shell metacharacters that must not appear in any argument.
# Space is intentionally excluded: create_subprocess_exec does not use a shell,
# so spaces in arguments are safe and rejecting them breaks valid paths.
_SHELL_METACHARS: str = "`$()|;&><\n"


# ---------------------------------------------------------------------------
# Checker
# ---------------------------------------------------------------------------


class PythonInvariantChecker:
    """
    Python re-implementation of the four boundary invariants.

    Parameters
    ----------
    allowed_root:
        Absolute path to the filesystem root that all tool paths must remain within.
    allowed_commands:
        Exact command tokens that the exec tool is permitted to run.
    """

    def __init__(self, allowed_root: str, allowed_commands: list[str]) -> None:
        self._allowed_root = Path(allowed_root).resolve()
        self._allowed_commands: frozenset[str] = frozenset(allowed_commands)

    # ------------------------------------------------------------------
    # Individual invariant checks
    # ------------------------------------------------------------------

    def check_path_confinement(self, path: str) -> None:
        """Raise if *path* escapes the allowed root.

        Performs lexical normalization (``os.path.normpath``) first, then an
        ``os.path.realpath`` call when the path exists on disk, mirroring the
        Rust implementation.
        """
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self._allowed_root / candidate

        # Prefer canonical resolution when path exists; fall back to normpath.
        try:
            if candidate.exists():
                resolved = Path(os.path.realpath(candidate))
            else:
                resolved = Path(os.path.normpath(candidate))
        except OSError:
            # Lexical fallback when permissions prevent stat (e.g., uv/cargo caches)
            resolved = Path(os.path.normpath(candidate))

        try:
            resolved.relative_to(self._allowed_root)
        except ValueError:
            raise InvariantViolation(
                invariant="PathConfinementInvariant",
                message=(f"path '{path}' escapes allowed root '{self._allowed_root}'"),
            ) from None

    def check_command_allowlist(self, command: str) -> None:
        """Raise if the leading token of *command* is not in the allowlist."""
        tokens = command.split()
        cmd = tokens[0].strip() if tokens else ""
        if not cmd:
            raise InvariantViolation(
                invariant="CommandAllowlistInvariant",
                message="empty command string",
            )
        if cmd not in self._allowed_commands:
            raise InvariantViolation(
                invariant="CommandAllowlistInvariant",
                message=f"command '{cmd}' is not in the allowlist",
            )

    def check_no_shell_metachars(self, args: list[str]) -> None:
        """Raise if any argument contains a shell metacharacter."""
        for arg in args:
            for ch in _SHELL_METACHARS:
                if ch in arg:
                    raise InvariantViolation(
                        invariant="NoShellMetacharInvariant",
                        message=(f"argument contains shell metacharacter {ch!r}"),
                    )

    def check_tool_name_known(self, tool_name: str) -> None:
        """Raise if *tool_name* is not a registered runner tool."""
        if tool_name not in _KNOWN_TOOLS:
            raise InvariantViolation(
                invariant="ToolNameKnownInvariant",
                message=f"unknown tool '{tool_name}'",
            )

    # ------------------------------------------------------------------
    # Composite check
    # ------------------------------------------------------------------

    def check_all(
        self,
        tool_name: str,
        path: str | None,
        command: str | None,
        args: list[str],
    ) -> None:
        """Run all applicable invariants in declaration order.

        Raises the first ``InvariantViolation`` found.
        """
        self.check_tool_name_known(tool_name)
        if path is not None:
            self.check_path_confinement(path)
        if command is not None:
            self.check_command_allowlist(command)
        self.check_no_shell_metachars(args)
