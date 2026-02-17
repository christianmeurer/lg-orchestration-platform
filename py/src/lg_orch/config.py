from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


class ConfigError(ValueError):
    pass


def _require_str(tbl: dict[str, object], key: str) -> str:
    v = tbl.get(key)
    if not isinstance(v, str) or not v.strip():
        raise ConfigError(f"missing/invalid {key}")
    return v


def _require_int(tbl: dict[str, object], key: str) -> int:
    v = tbl.get(key)
    if isinstance(v, bool):
        raise ConfigError(f"missing/invalid {key}")
    if v is None:
        raise ConfigError(f"missing/invalid {key}")
    try:
        if isinstance(v, int):
            return v
        if isinstance(v, float):
            return int(v)
        if isinstance(v, str):
            return int(v.strip())
        raise ConfigError(f"missing/invalid {key}")
    except Exception as exc:
        raise ConfigError(f"missing/invalid {key}") from exc


def _require_bool(tbl: dict[str, object], key: str) -> bool:
    v = tbl.get(key)
    if not isinstance(v, bool):
        raise ConfigError(f"missing/invalid {key}")
    return v


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
    api_key: str | None


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
    try:
        raw = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid toml: {cfg_path}") from exc

    if not isinstance(raw, dict):
        raise ConfigError("invalid config root")

    budgets_raw = raw.get("budgets")
    policy_raw = raw.get("policy")
    runner_raw = raw.get("runner")
    trace_raw = raw.get("trace", {})
    if not isinstance(budgets_raw, dict):
        raise ConfigError("missing/invalid budgets")
    if not isinstance(policy_raw, dict):
        raise ConfigError("missing/invalid policy")
    if not isinstance(runner_raw, dict):
        raise ConfigError("missing/invalid runner")
    if not isinstance(trace_raw, dict):
        raise ConfigError("missing/invalid trace")

    budgets = Budgets(
        max_loops=_require_int(budgets_raw, "max_loops"),
        max_tool_calls_per_loop=_require_int(budgets_raw, "max_tool_calls_per_loop"),
        max_patch_bytes=_require_int(budgets_raw, "max_patch_bytes"),
        tool_timeout_s=_require_int(budgets_raw, "tool_timeout_s"),
    )
    if budgets.max_loops < 1:
        raise ConfigError("budgets.max_loops must be >= 1")
    if budgets.max_tool_calls_per_loop < 0:
        raise ConfigError("budgets.max_tool_calls_per_loop must be >= 0")
    if budgets.max_patch_bytes < 1:
        raise ConfigError("budgets.max_patch_bytes must be >= 1")
    if budgets.tool_timeout_s < 1:
        raise ConfigError("budgets.tool_timeout_s must be >= 1")

    policy = Policy(
        network_default=_require_str(policy_raw, "network_default"),
        require_approval_for_mutations=_require_bool(policy_raw, "require_approval_for_mutations"),
    )
    if policy.network_default not in {"allow", "deny"}:
        raise ConfigError("policy.network_default must be one of: allow, deny")

    api_key = runner_raw.get("api_key")
    if api_key is None:
        api_key = os.environ.get("LG_RUNNER_API_KEY")
    if api_key is not None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ConfigError("missing/invalid runner.api_key")
        api_key = api_key.strip()
    runner = Runner(
        base_url=_require_str(runner_raw, "base_url"),
        root_dir=_require_str(runner_raw, "root_dir"),
        api_key=api_key,
    )
    if not (runner.base_url.startswith("http://") or runner.base_url.startswith("https://")):
        raise ConfigError("runner.base_url must start with http:// or https://")

    trace = Trace(
        enabled=bool(trace_raw.get("enabled", False)),
        output_dir=str(trace_raw.get("output_dir", "artifacts/runs")),
    )
    return AppConfig(profile=profile, budgets=budgets, policy=policy, runner=runner, trace=trace)
