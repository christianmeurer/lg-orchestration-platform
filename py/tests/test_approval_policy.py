from __future__ import annotations

from lg_orch.approval_policy import (
    ApprovalEngine,
    ApprovalVote,
    QuorumApprovalPolicy,
    RoleApprovalPolicy,
    TimedApprovalPolicy,
)


def _vote(
    reviewer_id: str,
    action: str,
    role: str | None = None,
) -> ApprovalVote:
    return ApprovalVote(
        reviewer_id=reviewer_id,
        role=role,
        action=action,  # type: ignore[arg-type]
        timestamp=0.0,
    )


engine = ApprovalEngine()


# ---------------------------------------------------------------------------
# TimedApprovalPolicy
# ---------------------------------------------------------------------------


def test_timed_policy_pending_before_timeout() -> None:
    policy = TimedApprovalPolicy(timeout_seconds=60.0, auto_action="reject")
    result = engine.evaluate(policy, [], elapsed_seconds=30.0)
    assert result == "pending"


def test_timed_policy_auto_reject_on_timeout() -> None:
    policy = TimedApprovalPolicy(timeout_seconds=60.0, auto_action="reject")
    result = engine.evaluate(policy, [], elapsed_seconds=60.0)
    assert result == "timed_out"


def test_timed_policy_auto_approve_on_timeout() -> None:
    policy = TimedApprovalPolicy(timeout_seconds=60.0, auto_action="approve")
    result = engine.evaluate(policy, [], elapsed_seconds=61.0)
    assert result == "approved"


# ---------------------------------------------------------------------------
# QuorumApprovalPolicy
# ---------------------------------------------------------------------------


def test_quorum_policy_approved_when_threshold_met() -> None:
    policy = QuorumApprovalPolicy(required_approvals=2, required_rejections=2)
    votes = [_vote("alice", "approve"), _vote("bob", "approve")]
    result = engine.evaluate(policy, votes, elapsed_seconds=0.0)
    assert result == "approved"


def test_quorum_policy_rejected_when_threshold_met() -> None:
    policy = QuorumApprovalPolicy(required_approvals=2, required_rejections=1)
    votes = [_vote("alice", "reject")]
    result = engine.evaluate(policy, votes, elapsed_seconds=0.0)
    assert result == "rejected"


def test_quorum_policy_pending_when_below_threshold() -> None:
    policy = QuorumApprovalPolicy(required_approvals=3, required_rejections=3)
    votes = [_vote("alice", "approve"), _vote("bob", "approve")]
    result = engine.evaluate(policy, votes, elapsed_seconds=0.0)
    assert result == "pending"


def test_quorum_policy_filters_by_allowed_reviewers() -> None:
    policy = QuorumApprovalPolicy(
        required_approvals=1,
        required_rejections=1,
        allowed_reviewers=["alice"],
    )
    # bob is not in allowed_reviewers — his vote is discarded
    votes = [_vote("bob", "approve")]
    result = engine.evaluate(policy, votes, elapsed_seconds=0.0)
    assert result == "pending"

    # alice IS in allowed_reviewers — her vote counts
    votes2 = [_vote("alice", "approve")]
    result2 = engine.evaluate(policy, votes2, elapsed_seconds=0.0)
    assert result2 == "approved"


# ---------------------------------------------------------------------------
# RoleApprovalPolicy
# ---------------------------------------------------------------------------


def test_role_policy_approved_when_any_role_votes() -> None:
    policy = RoleApprovalPolicy(required_roles=["admin", "security"], require_all_roles=False)
    votes = [_vote("alice", "approve", role="admin")]
    result = engine.evaluate(policy, votes, elapsed_seconds=0.0)
    assert result == "approved"


def test_role_policy_approved_when_all_roles_vote() -> None:
    policy = RoleApprovalPolicy(required_roles=["admin", "security"], require_all_roles=True)
    votes = [
        _vote("alice", "approve", role="admin"),
        _vote("bob", "approve", role="security"),
    ]
    result = engine.evaluate(policy, votes, elapsed_seconds=0.0)
    assert result == "approved"


def test_role_policy_rejected_immediately_on_rejection_from_required_role() -> None:
    policy = RoleApprovalPolicy(required_roles=["admin"], require_all_roles=False)
    votes = [_vote("alice", "reject", role="admin")]
    result = engine.evaluate(policy, votes, elapsed_seconds=0.0)
    assert result == "rejected"


def test_role_policy_pending_when_no_required_role_voted() -> None:
    policy = RoleApprovalPolicy(required_roles=["admin", "security"], require_all_roles=False)
    # vote from a role that is not in required_roles
    votes = [_vote("eve", "approve", role="viewer")]
    result = engine.evaluate(policy, votes, elapsed_seconds=0.0)
    assert result == "pending"
