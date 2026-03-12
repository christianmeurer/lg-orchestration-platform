from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Intent = Literal["code_change", "analysis", "research", "question", "refactor", "debug"]
RouteLane = Literal["interactive", "deep_planning", "recovery"]
RetryTarget = Literal["planner", "context_builder", "router"]
ContextScope = Literal["stable_prefix", "working_set", "full_reset"]
PlanAction = Literal["keep", "amend", "discard_reset"]


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: str
    input: dict[str, Any] = Field(default_factory=dict)


class PlanStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    description: str
    tools: list[ToolCall] = Field(default_factory=list)
    expected_outcome: str
    files_touched: list[str] = Field(default_factory=list)


class RecoveryAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    failure_class: str = ""
    failure_fingerprint: str = ""
    rationale: str = ""
    retry_target: RetryTarget = "planner"
    context_scope: ContextScope = "working_set"
    plan_action: PlanAction = "keep"


class PlannerOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    steps: list[PlanStep]
    verification: list[ToolCall]
    rollback: str
    acceptance_criteria: list[str] = Field(default_factory=list)
    max_iterations: int = Field(default=1, ge=1)
    recovery: RecoveryAction | None = None


class RouterDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: Intent
    task_class: str = "analysis"
    lane: RouteLane = "interactive"
    rationale: str = ""
    context_scope: ContextScope = "stable_prefix"
    latency_sensitive: bool = True
    cache_affinity: str = ""
    prefix_segment: str = "stable_prefix"
    provider_used: Literal["local", "remote"] = "local"
    provider: str = ""
    model: str = ""


class VerificationCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    ok: bool
    tool: str
    exit_code: int
    summary: str = ""


class VerifierReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    checks: list[VerificationCheck]
    retry_target: RetryTarget | None = None
    plan_action: PlanAction = "keep"
    failure_class: str = ""
    failure_fingerprint: str = ""
    recovery: RecoveryAction | None = None
    loop_summary: str = ""


class OrchState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request: str
    intent: Intent = "analysis"
    repo_context: dict[str, Any] = Field(default_factory=dict)
    facts: list[dict[str, Any]] = Field(default_factory=list)
    plan: PlannerOutput | None = None
    tool_results: list[dict[str, Any]] = Field(default_factory=list)
    patches: list[dict[str, Any]] = Field(default_factory=list)
    verification: VerifierReport | None = None
    final: str = ""
    guards: dict[str, Any] = Field(default_factory=dict)
    budgets: dict[str, Any] = Field(default_factory=dict)
    approvals: dict[str, Any] = Field(default_factory=dict)
    security: dict[str, Any] = Field(default_factory=dict)
    telemetry: dict[str, Any] = Field(default_factory=dict)
    route: RouterDecision | None = None
    retry_target: RetryTarget | None = None
    context_reset_requested: bool = False
    plan_discarded: bool = False
    plan_discard_reason: str = ""
    halt_reason: str = ""
    loop_summaries: list[dict[str, Any]] = Field(default_factory=list)
    history_policy: dict[str, Any] = Field(default_factory=dict)
    provenance: list[dict[str, Any]] = Field(default_factory=list)
    checkpoint: dict[str, Any] = Field(default_factory=dict)
    snapshots: list[dict[str, Any]] = Field(default_factory=list)
    undo: dict[str, Any] = Field(default_factory=dict)
    resume: dict[str, Any] = Field(default_factory=dict)


class ModelRoutingDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_class: str
    lane: RouteLane = "interactive"
    provider_used: Literal["local", "remote"]
    provider: str
    model: str
    reason: str
    fallback_applied: bool
    cache_affinity: str = ""
    prefix_segment: str = ""
    context_tokens: int = 0
    retry_count: int = 0
    latency_sensitive: bool = True


@dataclass(frozen=True)
class NodeResult:
    update: dict[str, Any]
