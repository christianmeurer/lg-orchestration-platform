from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Budgets:
    max_loops: int
    max_tool_calls_per_loop: int
    max_patch_bytes: int
    tool_timeout_s: int


@dataclass(frozen=True)
class Policy:
    network_default: str
    require_approval_for_mutations: bool


@dataclass(frozen=True)
class Runner:
    base_url: str
    root_dir: str


@dataclass(frozen=True)
class Trace:
    enabled: bool
    output_dir: str


@dataclass(frozen=True)
class AppConfig:
    profile: str
    budgets: Budgets
    policy: Policy
    runner: Runner
    trace: Trace


def load_config(*, repo_root: Path) -> AppConfig:
    profile = os.environ.get("LG_PROFILE", "dev").strip() or "dev"
    cfg_path = repo_root / "configs" / f"runtime.{profile}.toml"
    raw = tomllib.loads(cfg_path.read_text(encoding="utf-8"))

    budgets_raw = raw["budgets"]
    policy_raw = raw["policy"]
    runner_raw = raw["runner"]
    trace_raw = dict(raw.get("trace", {}))

    budgets = Budgets(
        max_loops=int(budgets_raw["max_loops"]),
        max_tool_calls_per_loop=int(budgets_raw["max_tool_calls_per_loop"]),
        max_patch_bytes=int(budgets_raw["max_patch_bytes"]),
        tool_timeout_s=int(budgets_raw["tool_timeout_s"]),
    )
    policy = Policy(
        network_default=str(policy_raw["network_default"]),
        require_approval_for_mutations=bool(policy_raw["require_approval_for_mutations"]),
    )
    runner = Runner(base_url=str(runner_raw["base_url"]), root_dir=str(runner_raw["root_dir"]))
    trace = Trace(
        enabled=bool(trace_raw.get("enabled", False)),
        output_dir=str(trace_raw.get("output_dir", "artifacts/runs")),
    )
    return AppConfig(profile=profile, budgets=budgets, policy=policy, runner=runner, trace=trace)
