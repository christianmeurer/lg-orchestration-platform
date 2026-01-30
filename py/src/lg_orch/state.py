from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Intent = Literal["code_change", "analysis", "research", "question", "refactor", "debug"]


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


class PlannerOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    steps: list[PlanStep]
    verification: list[ToolCall]
    rollback: str


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


@dataclass(frozen=True)
class NodeResult:
    update: dict[str, Any]
