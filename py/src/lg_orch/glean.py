# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Christian Meurer — https://github.com/christianmeurer/Lula
"""GLEAN — Guideline-grounded agent verification framework.

GLEAN (Guideline-grounded Learning and Evidence Accumulation for Norms)
provides a lightweight pre/post-execution auditing layer for agent tool calls.
Each registered Guideline is checked against tool arguments (pre) or outputs
(post) via a regex pattern.  Blocking violations halt execution; warnings and
errors are recorded for the run summary.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


@dataclass
class Guideline:
    """A single verifiable norm applied to tool calls."""

    id: str
    description: str
    check: str  # "pre" or "post"
    pattern: str  # regex applied to tool_args or result (as string)
    severity: str  # "warning", "error", or "block"


@dataclass
class GuidelineViolation:
    """Records a single guideline violation."""

    guideline_id: str
    tool_name: str
    detail: str
    severity: str


# ---------------------------------------------------------------------------
# Default guidelines
# ---------------------------------------------------------------------------

DEFAULT_GUIDELINES: list[Guideline] = [
    Guideline(
        id="no-force-push",
        description="Prevent force-pushing to remote git branches, which can destroy history.",
        check="pre",
        pattern=r"git\s+push\s+.*--force|git\s+push\s+.*-f\b",
        severity="block",
    ),
    Guideline(
        id="no-rm-rf-root",
        description="Prevent recursive deletion of root or home directories.",
        check="pre",
        pattern=r"rm\s+.*-[a-zA-Z]*r[a-zA-Z]*f[a-zA-Z]*\s+/\W|rm\s+.*-[a-zA-Z]*f[a-zA-Z]*r[a-zA-Z]*\s+/\W|rm\s+.*-rf\s+~",
        severity="block",
    ),
    Guideline(
        id="no-secret-in-stdout",
        description="Warn when output may contain secrets (API keys, passwords, tokens).",
        check="post",
        pattern=r"(?i)(api[_-]?key|secret|password|token|bearer)\s*[=:]\s*\S{8,}",
        severity="warning",
    ),
]


# ---------------------------------------------------------------------------
# Auditor
# ---------------------------------------------------------------------------


class GleanAuditor:
    """Runtime auditor that checks tool calls against registered guidelines."""

    def __init__(self) -> None:
        self._guidelines: list[Guideline] = []
        self._violations: list[GuidelineViolation] = []
        self._evidence: list[dict[str, Any]] = []

    def add_guideline(self, guideline: Guideline) -> None:
        """Register a guideline with the auditor."""
        self._guidelines.append(guideline)

    def check_pre_execution(
        self, tool_name: str, tool_args: dict[str, Any]
    ) -> list[GuidelineViolation]:
        """Check pre-execution guidelines against tool arguments.

        Returns only *blocking* violations so callers can halt immediately.
        All violations (including non-blocking) are recorded internally.
        """
        subject = str(tool_args)
        blocking: list[GuidelineViolation] = []
        for guideline in self._guidelines:
            if guideline.check != "pre":
                continue
            if re.search(guideline.pattern, subject):
                violation = GuidelineViolation(
                    guideline_id=guideline.id,
                    tool_name=tool_name,
                    detail=(
                        f"pre-execution pattern '{guideline.pattern}' matched args: {subject[:200]}"
                    ),
                    severity=guideline.severity,
                )
                self._violations.append(violation)
                logger.warning(
                    "glean_violation",
                    check="pre",
                    guideline_id=guideline.id,
                    tool_name=tool_name,
                    severity=guideline.severity,
                )
                if guideline.severity == "block":
                    blocking.append(violation)
        return blocking

    def check_post_execution(self, tool_name: str, result: Any) -> list[GuidelineViolation]:
        """Check post-execution guidelines against tool result."""
        subject = str(result)
        violations: list[GuidelineViolation] = []
        for guideline in self._guidelines:
            if guideline.check != "post":
                continue
            if re.search(guideline.pattern, subject):
                violation = GuidelineViolation(
                    guideline_id=guideline.id,
                    tool_name=tool_name,
                    detail=(
                        f"post-execution pattern '{guideline.pattern}' matched"
                        f" result: {subject[:200]}"
                    ),
                    severity=guideline.severity,
                )
                self._violations.append(violation)
                violations.append(violation)
                logger.warning(
                    "glean_violation",
                    check="post",
                    guideline_id=guideline.id,
                    tool_name=tool_name,
                    severity=guideline.severity,
                )
        return violations

    def record_evidence(self, tool_name: str, action: str, detail: str) -> None:
        """Record a positive evidence entry for audit trails."""
        entry: dict[str, Any] = {
            "tool_name": tool_name,
            "action": action,
            "detail": detail,
        }
        self._evidence.append(entry)
        logger.debug("glean_evidence", tool_name=tool_name, action=action)

    def summary(self) -> dict[str, Any]:
        """Return a summary of the audit session."""
        blocking = [v for v in self._violations if v.severity == "block"]
        return {
            "guidelines_checked": len(self._guidelines),
            "violations": len(self._violations),
            "blocking_violations": len(blocking),
            "evidence_entries": len(self._evidence),
            "compliant": len(blocking) == 0,
        }
