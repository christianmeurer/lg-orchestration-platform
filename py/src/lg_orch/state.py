# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Christian Meurer — https://github.com/christianmeurer/Lula
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field

from lg_orch.approval_policy import ApprovalPolicy, ApprovalVote

Intent = Literal["code_change", "analysis", "research", "question", "refactor", "debug"]
RouteLane = Literal["interactive", "deep_planning", "recovery"]
RetryTarget = Literal["planner", "coder", "context_builder", "router"]
ContextScope = Literal["stable_prefix", "working_set", "full_reset"]
PlanAction = Literal["keep", "amend", "discard_reset"]


class HandoffEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    detail: str
    ref: str = ""


class AgentHandoff(BaseModel):
    model_config = ConfigDict(extra="forbid")

    producer: str
    consumer: str
    objective: str
    file_scope: list[str] = Field(default_factory=list)
    evidence: list[HandoffEvidence] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    acceptance_checks: list[str] = Field(default_factory=list)
    retry_budget: int = Field(default=1, ge=0)
    provenance: list[str] = Field(default_factory=list)


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: str
    input: dict[str, Any] = Field(default_factory=dict)


class PlanStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    description: str = ""
    tools: list[ToolCall] = Field(default_factory=list)
    expected_outcome: str = ""
    files_touched: list[str] = Field(default_factory=list)
    handoff: AgentHandoff | None = None


class RecoveryAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    failure_class: str = ""
    failure_fingerprint: str = ""
    rationale: str = ""
    retry_target: RetryTarget = "planner"
    context_scope: ContextScope = "working_set"
    plan_action: PlanAction = "keep"


class RecoveryPacket(RecoveryAction):
    loop: int = 0
    origin: str = "verifier"
    summary: str = ""
    last_check: str = ""
    discard_reason: str = ""


class PlannerOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    steps: list[PlanStep]
    verification: list[ToolCall]
    rollback: str
    acceptance_criteria: list[str] = Field(default_factory=list)
    max_iterations: int = Field(default=1, ge=1)
    recovery: RecoveryAction | None = None
    recovery_packet: RecoveryPacket | None = None


class RouterDecision(BaseModel):
    model_config = ConfigDict(extra="ignore")

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
    context_tokens: int = 0
    compression_pressure: int = 0
    fact_count: int = 0


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
    acceptance_ok: bool = True
    acceptance_checks: list[dict[str, Any]] = Field(default_factory=list)
    retry_target: RetryTarget | None = None
    plan_action: PlanAction = "keep"
    failure_class: str = ""
    failure_fingerprint: str = ""
    recovery: RecoveryAction | None = None
    recovery_packet: RecoveryPacket | None = None
    next_handoff: AgentHandoff | None = None
    loop_summary: str = ""


class OrchStateDict(TypedDict, total=False):
    request: str
    intent: Intent
    repo_context: dict[str, Any]
    facts: list[dict[str, Any]]
    plan: PlannerOutput | None
    tool_results: list[dict[str, Any]]
    patches: list[dict[str, Any]]
    verification: VerifierReport | None
    final: str
    guards: dict[str, Any]
    budgets: dict[str, Any]
    approvals: dict[str, Any]
    security: dict[str, Any]
    telemetry: dict[str, Any]
    route: RouterDecision | None
    active_handoff: AgentHandoff | None
    retry_target: RetryTarget | None
    recovery_packet: RecoveryPacket | None
    context_reset_requested: bool
    plan_discarded: bool
    plan_discard_reason: str
    halt_reason: str
    loop_summaries: list[dict[str, Any]]
    history_policy: dict[str, Any]
    provenance: list[dict[str, Any]]
    checkpoint: dict[str, Any]
    snapshots: list[dict[str, Any]]
    undo: dict[str, Any]
    resume: dict[str, Any]
    mcp_tools: list[dict[str, Any]]
    worktree_path: str | None
    long_term_memory_path: str | None
    repo_root: str | None
    runner_url: str | None
    healing_job_id: str | None
    test_repair_mode: bool
    _run_id: str
    _repo_root: str
    _runner_base_url: str
    _runner_api_key: str | None
    _mcp_enabled: bool
    _mcp_servers: dict[str, Any]
    _checkpoint: dict[str, Any]
    _models: dict[str, Any]
    _model_routing_policy: dict[str, Any]
    _model_provider_runtime: dict[str, Any]
    _budget_max_loops: int
    _budget_max_tool_calls_per_loop: int
    _budget_max_patch_bytes: int
    _budget_context: dict[str, Any]
    _config_policy: dict[str, Any]
    _trace_enabled: bool
    _trace_out_dir: str
    _trace_capture_model_metadata: bool
    _run_store_path: str
    _procedure_cache_path: str
    _vericoding_enabled: bool
    _vericoding_extensions: list[str]
    _request_id: str
    _remote_api_context: dict[str, Any]
    _resume_approvals: dict[str, Any]
    _approval_context: dict[str, Any]
    _runner_enabled: bool
    _trace_events: list[dict[str, Any]]


class OrchState(BaseModel):
    # extra="allow" is required so that LangGraph's internal underscore-prefixed
    # fields (_run_id, _lane, etc.) can coexist in the graph state without
    # causing Pydantic validation errors.
    model_config = ConfigDict(extra="allow", populate_by_name=True)

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
    active_handoff: AgentHandoff | None = None
    retry_target: RetryTarget | None = None
    recovery_packet: RecoveryPacket | None = None
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
    mcp_tools: list[dict[str, Any]] = Field(default_factory=list)
    worktree_path: str | None = None
    long_term_memory_path: str | None = None
    repo_root: str | None = None
    runner_url: str | None = None
    healing_job_id: str | None = None
    test_repair_mode: bool = False

    # -------------------------------------------------------------------------
    # Internal fields using aliases because Pydantic ignores _ prefixed fields
    # during initialization and model_dump(), which breaks LangGraph state!
    # -------------------------------------------------------------------------
    run_id_internal: str = Field(default="", alias="_run_id")
    repo_root_internal: str = Field(default="", alias="_repo_root")
    runner_base_url_internal: str = Field(default="http://127.0.0.1:8088", alias="_runner_base_url")
    runner_api_key_internal: str | None = Field(default=None, alias="_runner_api_key")
    mcp_enabled_internal: bool = Field(default=False, alias="_mcp_enabled")
    mcp_servers_internal: dict[str, Any] = Field(default_factory=dict, alias="_mcp_servers")
    checkpoint_internal: dict[str, Any] = Field(default_factory=dict, alias="_checkpoint")
    models_internal: dict[str, Any] = Field(default_factory=dict, alias="_models")
    model_routing_policy_internal: dict[str, Any] = Field(
        default_factory=dict, alias="_model_routing_policy"
    )
    model_provider_runtime_internal: dict[str, Any] = Field(
        default_factory=dict, alias="_model_provider_runtime"
    )
    budget_max_loops_internal: int = Field(
        default=1, alias="_budget_max_loops"
    )
    budget_max_tool_calls_per_loop_internal: int = Field(
        default=1, alias="_budget_max_tool_calls_per_loop"
    )
    budget_max_patch_bytes_internal: int = Field(
        default=0, alias="_budget_max_patch_bytes"
    )
    budget_context_internal: dict[str, Any] = Field(
        default_factory=dict, alias="_budget_context"
    )
    config_policy_internal: dict[str, Any] = Field(
        default_factory=dict, alias="_config_policy"
    )
    trace_enabled_internal: bool = Field(default=False, alias="_trace_enabled")
    trace_out_dir_internal: str = Field(default="", alias="_trace_out_dir")
    trace_capture_model_metadata_internal: bool = Field(
        default=False, alias="_trace_capture_model_metadata"
    )
    run_store_path_internal: str = Field(default="", alias="_run_store_path")
    procedure_cache_path_internal: str = Field(
        default="", alias="_procedure_cache_path"
    )
    vericoding_enabled_internal: bool = Field(
        default=False, alias="_vericoding_enabled"
    )
    vericoding_extensions_internal: list[str] = Field(
        default_factory=list, alias="_vericoding_extensions"
    )
    request_id_internal: str = Field(default="", alias="_request_id")
    remote_api_context_internal: dict[str, Any] = Field(
        default_factory=dict, alias="_remote_api_context"
    )
    resume_approvals_internal: dict[str, Any] = Field(
        default_factory=dict, alias="_resume_approvals"
    )
    approval_context_internal: dict[str, Any] = Field(
        default_factory=dict, alias="_approval_context"
    )
    runner_enabled_internal: bool = Field(default=True, alias="_runner_enabled")
    trace_events_internal: list[dict[str, Any]] = Field(default_factory=list, alias="_trace_events")


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
    compression_pressure: int = 0
    fact_count: int = 0
    retry_count: int = 0
    latency_sensitive: bool = True


@dataclass(frozen=True)
class NodeResult:
    update: dict[str, Any]


@dataclass
class ApprovalRecord:
    run_id: str
    status: Literal["pending", "approved", "rejected", "timed_out"] = "pending"
    policy: ApprovalPolicy | None = None
    votes: list[ApprovalVote] = field(default_factory=list)


class SubAgentTask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    repository: str
    objective: str
    dependencies: list[str] = Field(default_factory=list)


class SubAgentResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str
    status: Literal["success", "failure"]
    output: str
    pr_url: str | None = None
    diff: str | None = None


class MetaPlanOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    global_objective: str
    sub_tasks: list[SubAgentTask]
    resolution_criteria: list[str]


class MetaOrchState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request: str
    repositories: list[str] = Field(default_factory=list)
    meta_plan: MetaPlanOutput | None = None
    task_results: dict[str, SubAgentResult] = Field(default_factory=dict)
    active_tasks: list[str] = Field(default_factory=list)
    completed_tasks: list[str] = Field(default_factory=list)
    failed_tasks: list[str] = Field(default_factory=list)
    final_report: str = ""
    error: str = ""


def validate_state(state: dict[str, Any]) -> OrchState:
    """Coerce a raw dict into a validated :class:`OrchState`.

    Used by the ``ingest`` node as the authoritative entry-point into the
    typed state pipeline.  Raises ``ValidationError`` on invalid input.
    """
    return OrchState.model_validate(state)
