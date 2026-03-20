# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Christian Meurer — https://github.com/christianmeurer/Lula
from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any

from pydantic import Field, TypeAdapter


@dataclass(frozen=True)
class PolicyDecision:
    allow_network: bool
    require_approval_for_mutations: bool
    allowed_write_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class LoopBudgetDecision:
    allow: bool
    current_loop: int
    max_loops: int
    halt_reason: str


def decide_policy(
    *,
    network_default: str,
    require_approval_for_mutations: bool,
    allowed_write_paths: tuple[str, ...] = (),
) -> PolicyDecision:
    allow_network = network_default.strip().lower() == "allow"
    return PolicyDecision(
        allow_network=allow_network,
        require_approval_for_mutations=require_approval_for_mutations,
        allowed_write_paths=tuple(path.strip() for path in allowed_write_paths if path.strip()),
    )


_NON_NEG_INT_ADAPTER: TypeAdapter[int] = TypeAdapter(Annotated[int, Field(ge=0)])


def _coerce_non_negative_int(value: Any, *, fallback: int) -> int:
    if isinstance(value, bool):
        return fallback
    try:
        if isinstance(value, str):
            value = value.strip()
        return _NON_NEG_INT_ADAPTER.validate_python(value)
    except Exception:
        return fallback


def enforce_loop_budget(
    *, budgets: dict[str, Any], configured_max_loops: int, plan_max_iterations: int | None = None
) -> LoopBudgetDecision:
    safe_max = configured_max_loops if configured_max_loops >= 1 else 1
    effective_max = safe_max
    plan_limit = None
    if isinstance(plan_max_iterations, int) and not isinstance(plan_max_iterations, bool):
        if plan_max_iterations >= 1:
            plan_limit = plan_max_iterations
            effective_max = min(effective_max, plan_limit)
    max_loops = _coerce_non_negative_int(budgets.get("max_loops"), fallback=effective_max)
    if max_loops < 1:
        max_loops = effective_max
    max_loops = min(max_loops, effective_max)

    current_loop = _coerce_non_negative_int(budgets.get("current_loop"), fallback=0)
    if current_loop >= max_loops:
        halt_reason = "max_loops_exhausted"
        if plan_limit is not None and max_loops == plan_limit:
            halt_reason = "plan_max_iterations_exhausted"
        return LoopBudgetDecision(
            allow=False,
            current_loop=current_loop,
            max_loops=max_loops,
            halt_reason=halt_reason,
        )

    next_loop = current_loop + 1
    return LoopBudgetDecision(
        allow=True,
        current_loop=next_loop,
        max_loops=max_loops,
        halt_reason="",
    )
