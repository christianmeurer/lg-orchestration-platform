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
    *, budgets: dict[str, Any], configured_max_loops: int
) -> LoopBudgetDecision:
    safe_max = configured_max_loops if configured_max_loops >= 1 else 1
    max_loops = _coerce_non_negative_int(budgets.get("max_loops"), fallback=safe_max)
    if max_loops < 1:
        max_loops = safe_max

    current_loop = _coerce_non_negative_int(budgets.get("current_loop"), fallback=0)
    if current_loop >= max_loops:
        return LoopBudgetDecision(
            allow=False,
            current_loop=current_loop,
            max_loops=max_loops,
            halt_reason="max_loops_exhausted",
        )

    next_loop = current_loop + 1
    return LoopBudgetDecision(
        allow=True,
        current_loop=next_loop,
        max_loops=max_loops,
        halt_reason="",
    )
