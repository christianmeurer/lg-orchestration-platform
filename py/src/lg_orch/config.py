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


def _get_int(tbl: dict[str, object], key: str, *, default: int) -> int:
    if key not in tbl:
        return default
    return _require_int(tbl, key)


def _optional_str_tuple(tbl: dict[str, object], key: str) -> tuple[str, ...]:
    raw = tbl.get(key, [])
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError(f"missing/invalid {key}")

    values: list[str] = []
    for entry in raw:
        if not isinstance(entry, str) or not entry.strip():
            raise ConfigError(f"missing/invalid {key}")
        values.append(entry.strip())
    return tuple(values)


@dataclass(frozen=True)
class Budgets:
    max_loops: int
    max_tool_calls_per_loop: int
    max_patch_bytes: int
    tool_timeout_s: int
    stable_prefix_tokens: int = 1600
    working_set_tokens: int = 1600
    tool_result_summary_chars: int = 480


@dataclass(frozen=True)
class Policy:
    network_default: str
    require_approval_for_mutations: bool
    allowed_write_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class Runner:
    base_url: str
    root_dir: str
    api_key: str | None


@dataclass(frozen=True)
class MCPServerConfig:
    command: str
    args: tuple[str, ...]
    cwd: str | None
    env: dict[str, str]
    timeout_s: int


@dataclass(frozen=True)
class MCPConfig:
    enabled: bool
    servers: dict[str, MCPServerConfig]


@dataclass(frozen=True)
class Trace:
    enabled: bool
    output_dir: str
    capture_model_metadata: bool = True


@dataclass(frozen=True)
class Checkpoint:
    enabled: bool
    db_path: str
    namespace: str
    thread_prefix: str


@dataclass(frozen=True)
class ModelEndpoint:
    provider: str
    model: str
    temperature: float


@dataclass(frozen=True)
class ModelRouting:
    local_provider: str
    fallback_task_classes: tuple[str, ...]
    interactive_context_limit: int = 1800
    deep_planning_context_limit: int = 3200
    recovery_retry_threshold: int = 1
    default_cache_affinity: str = "workspace"


@dataclass(frozen=True)
class Models:
    router: ModelEndpoint
    planner: ModelEndpoint
    routing: ModelRouting
    digitalocean: "DigitalOceanServerless"


@dataclass(frozen=True)
class DigitalOceanServerless:
    base_url: str
    api_key: str | None
    timeout_s: int


@dataclass(frozen=True)
class AppConfig:
    profile: str
    models: Models
    budgets: Budgets
    policy: Policy
    runner: Runner
    mcp: MCPConfig
    trace: Trace
    checkpoint: Checkpoint


def _parse_float(value: object, *, default: float) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return default
    return default


def _parse_model_endpoint(models_raw: dict[str, object], key: str) -> ModelEndpoint:
    section = models_raw.get(key)
    if not isinstance(section, dict):
        return ModelEndpoint(provider="local", model="deterministic", temperature=0.0)

    provider_raw = section.get("provider", "local")
    model_raw = section.get("model", "deterministic")
    provider = provider_raw.strip() if isinstance(provider_raw, str) else "local"
    model = model_raw.strip() if isinstance(model_raw, str) else "deterministic"
    if not provider:
        provider = "local"
    if not model:
        model = "deterministic"
    temperature = _parse_float(section.get("temperature", 0.0), default=0.0)

    return ModelEndpoint(provider=provider, model=model, temperature=temperature)


def _parse_model_routing(models_raw: dict[str, object]) -> ModelRouting:
    routing_raw = models_raw.get("routing")
    if not isinstance(routing_raw, dict):
        return ModelRouting(
            local_provider="local",
            fallback_task_classes=("summarization", "lint_reflection", "context_condensation"),
            interactive_context_limit=1800,
            deep_planning_context_limit=3200,
            recovery_retry_threshold=1,
            default_cache_affinity="workspace",
        )

    local_provider_raw = routing_raw.get("local_provider", "local")
    local_provider = local_provider_raw.strip() if isinstance(local_provider_raw, str) else "local"
    if not local_provider:
        local_provider = "local"

    raw_classes = routing_raw.get("fallback_task_classes", [])
    classes: list[str] = []
    if isinstance(raw_classes, list):
        for entry in raw_classes:
            if isinstance(entry, str):
                value = entry.strip()
                if value:
                    classes.append(value)

    if not classes:
        classes = ["summarization", "lint_reflection", "context_condensation"]

    interactive_context_limit = _get_int(
        routing_raw,
        "interactive_context_limit",
        default=1800,
    )
    deep_planning_context_limit = _get_int(
        routing_raw,
        "deep_planning_context_limit",
        default=3200,
    )
    recovery_retry_threshold = _get_int(
        routing_raw,
        "recovery_retry_threshold",
        default=1,
    )
    default_cache_affinity_raw = routing_raw.get("default_cache_affinity", "workspace")
    default_cache_affinity = (
        default_cache_affinity_raw.strip()
        if isinstance(default_cache_affinity_raw, str)
        else "workspace"
    )
    if not default_cache_affinity:
        default_cache_affinity = "workspace"

    if interactive_context_limit < 1:
        raise ConfigError("models.routing.interactive_context_limit must be >= 1")
    if deep_planning_context_limit < interactive_context_limit:
        raise ConfigError(
            "models.routing.deep_planning_context_limit must be >= models.routing.interactive_context_limit"
        )
    if recovery_retry_threshold < 0:
        raise ConfigError("models.routing.recovery_retry_threshold must be >= 0")

    return ModelRouting(
        local_provider=local_provider,
        fallback_task_classes=tuple(classes),
        interactive_context_limit=interactive_context_limit,
        deep_planning_context_limit=deep_planning_context_limit,
        recovery_retry_threshold=recovery_retry_threshold,
        default_cache_affinity=default_cache_affinity,
    )


def _parse_digitalocean_serverless(models_raw: dict[str, object]) -> DigitalOceanServerless:
    section = models_raw.get("digitalocean")
    section_dict = section if isinstance(section, dict) else {}

    base_url_raw = section_dict.get("base_url", "https://inference.do-ai.run/v1")
    if not isinstance(base_url_raw, str) or not base_url_raw.strip():
        raise ConfigError("missing/invalid models.digitalocean.base_url")
    base_url = base_url_raw.strip().rstrip("/")
    if not (base_url.startswith("http://") or base_url.startswith("https://")):
        raise ConfigError("models.digitalocean.base_url must start with http:// or https://")

    api_key_raw = section_dict.get("api_key")
    api_key: str | None
    if api_key_raw is None:
        api_key = os.environ.get("MODEL_ACCESS_KEY") or os.environ.get(
            "DIGITAL_OCEAN_MODEL_ACCESS_KEY"
        )
    elif isinstance(api_key_raw, str):
        api_key = api_key_raw.strip() or None
    else:
        raise ConfigError("missing/invalid models.digitalocean.api_key")
    if api_key is not None:
        api_key = api_key.strip() or None

    timeout_raw = section_dict.get("timeout_s", 60)
    if isinstance(timeout_raw, bool) or not isinstance(timeout_raw, int):
        raise ConfigError("missing/invalid models.digitalocean.timeout_s")
    if timeout_raw < 1:
        raise ConfigError("models.digitalocean.timeout_s must be >= 1")

    return DigitalOceanServerless(base_url=base_url, api_key=api_key, timeout_s=timeout_raw)


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
    models_raw = raw.get("models", {})
    policy_raw = raw.get("policy")
    runner_raw = raw.get("runner")
    mcp_raw = raw.get("mcp", {})
    trace_raw = raw.get("trace", {})
    checkpoint_raw = raw.get("checkpoint", {})
    if not isinstance(models_raw, dict):
        raise ConfigError("missing/invalid models")
    if not isinstance(budgets_raw, dict):
        raise ConfigError("missing/invalid budgets")
    if not isinstance(policy_raw, dict):
        raise ConfigError("missing/invalid policy")
    if not isinstance(runner_raw, dict):
        raise ConfigError("missing/invalid runner")
    if not isinstance(mcp_raw, dict):
        raise ConfigError("missing/invalid mcp")
    if not isinstance(trace_raw, dict):
        raise ConfigError("missing/invalid trace")
    if not isinstance(checkpoint_raw, dict):
        raise ConfigError("missing/invalid checkpoint")

    budgets = Budgets(
        max_loops=_require_int(budgets_raw, "max_loops"),
        max_tool_calls_per_loop=_require_int(budgets_raw, "max_tool_calls_per_loop"),
        max_patch_bytes=_require_int(budgets_raw, "max_patch_bytes"),
        tool_timeout_s=_require_int(budgets_raw, "tool_timeout_s"),
        stable_prefix_tokens=_get_int(budgets_raw, "stable_prefix_tokens", default=1600),
        working_set_tokens=_get_int(budgets_raw, "working_set_tokens", default=1600),
        tool_result_summary_chars=_get_int(budgets_raw, "tool_result_summary_chars", default=480),
    )
    if budgets.max_loops < 1:
        raise ConfigError("budgets.max_loops must be >= 1")
    if budgets.max_tool_calls_per_loop < 0:
        raise ConfigError("budgets.max_tool_calls_per_loop must be >= 0")
    if budgets.max_patch_bytes < 1:
        raise ConfigError("budgets.max_patch_bytes must be >= 1")
    if budgets.tool_timeout_s < 1:
        raise ConfigError("budgets.tool_timeout_s must be >= 1")
    if budgets.stable_prefix_tokens < 1:
        raise ConfigError("budgets.stable_prefix_tokens must be >= 1")
    if budgets.working_set_tokens < 1:
        raise ConfigError("budgets.working_set_tokens must be >= 1")
    if budgets.tool_result_summary_chars < 80:
        raise ConfigError("budgets.tool_result_summary_chars must be >= 80")

    models = Models(
        router=_parse_model_endpoint(models_raw, "router"),
        planner=_parse_model_endpoint(models_raw, "planner"),
        routing=_parse_model_routing(models_raw),
        digitalocean=_parse_digitalocean_serverless(models_raw),
    )

    policy = Policy(
        network_default=_require_str(policy_raw, "network_default"),
        require_approval_for_mutations=_require_bool(policy_raw, "require_approval_for_mutations"),
        allowed_write_paths=_optional_str_tuple(policy_raw, "allowed_write_paths"),
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

    mcp_enabled_raw = mcp_raw.get("enabled", False)
    if not isinstance(mcp_enabled_raw, bool):
        raise ConfigError("missing/invalid mcp.enabled")

    servers_raw = mcp_raw.get("servers", {})
    if not isinstance(servers_raw, dict):
        raise ConfigError("missing/invalid mcp.servers")

    servers: dict[str, MCPServerConfig] = {}
    for server_name, server_data in servers_raw.items():
        if not isinstance(server_name, str) or not server_name.strip():
            raise ConfigError("missing/invalid mcp.servers key")
        if not isinstance(server_data, dict):
            raise ConfigError(f"missing/invalid mcp.servers.{server_name}")

        command_raw = server_data.get("command")
        if not isinstance(command_raw, str) or not command_raw.strip():
            raise ConfigError(f"missing/invalid mcp.servers.{server_name}.command")

        args_raw = server_data.get("args", [])
        if not isinstance(args_raw, list):
            raise ConfigError(f"missing/invalid mcp.servers.{server_name}.args")
        args: list[str] = []
        for arg in args_raw:
            if not isinstance(arg, str):
                raise ConfigError(f"missing/invalid mcp.servers.{server_name}.args entry")
            args.append(arg)

        cwd_raw = server_data.get("cwd")
        cwd: str | None
        if cwd_raw is None:
            cwd = None
        elif isinstance(cwd_raw, str):
            cwd = cwd_raw.strip() or None
        else:
            raise ConfigError(f"missing/invalid mcp.servers.{server_name}.cwd")

        env_raw = server_data.get("env", {})
        if not isinstance(env_raw, dict):
            raise ConfigError(f"missing/invalid mcp.servers.{server_name}.env")
        env: dict[str, str] = {}
        for k, v in env_raw.items():
            if not isinstance(k, str) or not isinstance(v, str):
                raise ConfigError(f"missing/invalid mcp.servers.{server_name}.env entry")
            env[k] = v

        timeout_s_raw = server_data.get("timeout_s", 20)
        if isinstance(timeout_s_raw, bool) or not isinstance(timeout_s_raw, int):
            raise ConfigError(f"missing/invalid mcp.servers.{server_name}.timeout_s")
        if timeout_s_raw < 1:
            raise ConfigError(f"mcp.servers.{server_name}.timeout_s must be >= 1")

        servers[server_name.strip()] = MCPServerConfig(
            command=command_raw.strip(),
            args=tuple(args),
            cwd=cwd,
            env=env,
            timeout_s=timeout_s_raw,
        )

    mcp = MCPConfig(enabled=mcp_enabled_raw, servers=servers)

    trace = Trace(
        enabled=bool(trace_raw.get("enabled", False)),
        output_dir=str(trace_raw.get("output_dir", "artifacts/runs")),
        capture_model_metadata=bool(trace_raw.get("capture_model_metadata", True)),
    )

    checkpoint_enabled = checkpoint_raw.get("enabled", True)
    if not isinstance(checkpoint_enabled, bool):
        raise ConfigError("missing/invalid checkpoint.enabled")

    checkpoint_db_path_raw = checkpoint_raw.get("db_path", "artifacts/checkpoints/langgraph.sqlite")
    if not isinstance(checkpoint_db_path_raw, str) or not checkpoint_db_path_raw.strip():
        raise ConfigError("missing/invalid checkpoint.db_path")

    checkpoint_namespace_raw = checkpoint_raw.get("namespace", "main")
    if not isinstance(checkpoint_namespace_raw, str) or not checkpoint_namespace_raw.strip():
        raise ConfigError("missing/invalid checkpoint.namespace")

    checkpoint_thread_prefix_raw = checkpoint_raw.get("thread_prefix", "lg-orch")
    if (
        not isinstance(checkpoint_thread_prefix_raw, str)
        or not checkpoint_thread_prefix_raw.strip()
    ):
        raise ConfigError("missing/invalid checkpoint.thread_prefix")

    checkpoint = Checkpoint(
        enabled=checkpoint_enabled,
        db_path=checkpoint_db_path_raw.strip(),
        namespace=checkpoint_namespace_raw.strip(),
        thread_prefix=checkpoint_thread_prefix_raw.strip(),
    )

    return AppConfig(
        profile=profile,
        models=models,
        budgets=budgets,
        policy=policy,
        runner=runner,
        mcp=mcp,
        trace=trace,
        checkpoint=checkpoint,
    )
