from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class TimedApprovalPolicy:
    kind: Literal["timed"] = "timed"
    timeout_seconds: float = 300.0
    auto_action: Literal["approve", "reject"] = "reject"


@dataclass(frozen=True)
class QuorumApprovalPolicy:
    kind: Literal["quorum"] = "quorum"
    required_approvals: int = 1
    required_rejections: int = 1
    allowed_reviewers: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RoleApprovalPolicy:
    kind: Literal["role"] = "role"
    required_roles: list[str] = field(default_factory=list)
    require_all_roles: bool = False


ApprovalPolicy = TimedApprovalPolicy | QuorumApprovalPolicy | RoleApprovalPolicy


@dataclass
class ApprovalVote:
    reviewer_id: str
    role: str | None
    action: Literal["approve", "reject"]
    timestamp: float
    comment: str = ""


@dataclass
class ApprovalDecision:
    run_id: str
    status: Literal["pending", "approved", "rejected", "timed_out"]
    policy: ApprovalPolicy
    votes: list[ApprovalVote] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    resolved_at: float | None = None


class ApprovalEngine:
    """Stateless evaluator: given a policy and a list of votes (+ elapsed time),
    returns the current ApprovalDecision status."""

    def evaluate(
        self,
        policy: ApprovalPolicy,
        votes: list[ApprovalVote],
        elapsed_seconds: float,
    ) -> Literal["pending", "approved", "rejected", "timed_out"]:
        if isinstance(policy, TimedApprovalPolicy):
            return self._evaluate_timed(policy, elapsed_seconds)
        if isinstance(policy, QuorumApprovalPolicy):
            return self._evaluate_quorum(policy, votes)
        if isinstance(policy, RoleApprovalPolicy):
            return self._evaluate_role(policy, votes)
        return "pending"

    def _evaluate_timed(
        self,
        policy: TimedApprovalPolicy,
        elapsed_seconds: float,
    ) -> Literal["pending", "approved", "rejected", "timed_out"]:
        if elapsed_seconds >= policy.timeout_seconds:
            if policy.auto_action == "approve":
                return "approved"
            return "timed_out"
        return "pending"

    def _evaluate_quorum(
        self,
        policy: QuorumApprovalPolicy,
        votes: list[ApprovalVote],
    ) -> Literal["pending", "approved", "rejected", "timed_out"]:
        filtered: list[ApprovalVote]
        if policy.allowed_reviewers:
            allowed_set = set(policy.allowed_reviewers)
            filtered = [v for v in votes if v.reviewer_id in allowed_set]
        else:
            filtered = list(votes)

        approvals = sum(1 for v in filtered if v.action == "approve")
        rejections = sum(1 for v in filtered if v.action == "reject")

        if rejections >= policy.required_rejections:
            return "rejected"
        if approvals >= policy.required_approvals:
            return "approved"
        return "pending"

    def _evaluate_role(
        self,
        policy: RoleApprovalPolicy,
        votes: list[ApprovalVote],
    ) -> Literal["pending", "approved", "rejected", "timed_out"]:
        required_set = set(policy.required_roles)

        # Any rejection from a reviewer whose role is in required_roles → immediate rejected
        for vote in votes:
            if vote.action == "reject" and vote.role in required_set:
                return "rejected"

        approving_roles = {v.role for v in votes if v.action == "approve" and v.role in required_set}

        if policy.require_all_roles:
            if required_set and required_set.issubset(approving_roles):
                return "approved"
            return "pending"
        else:
            if approving_roles:
                return "approved"
            return "pending"
