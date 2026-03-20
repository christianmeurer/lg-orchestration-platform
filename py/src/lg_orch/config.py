from __future__ import annotations

import os
import re as _re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from lg_orch.audit import AuditConfig

_SHA256_RE = _re.compile(r'^[0-9a-f]{64}$')
_NAMESPACE_RE = _re.compile(r'^[A-Za-z0-9_-]{1,64}$')


def _is_valid_sha256_hex(value: str) -> bool:
    return bool(_SHA256_RE.fullmatch(value))


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


def _get_bool(tbl: dict[str, object], key: str, *, default: bool) -> bool:
    if key not in tbl:
        return default
    return _require_bool(tbl, key)


def _env_bool(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"missing/invalid env {name}")


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
    schema_hash: str | None = None  # SHA-256 of sorted tools/list JSON; None = unpinned


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
class RemoteAPIConfig:
    auth_mode: str = "off"
    bearer_token: str | None = None
    allow_unauthenticated_healthz: bool = True
    trust_forwarded_headers: bool = False
    access_log_enabled: bool = True
    run_store_path: str | None = None
    rate_limit_rps: int = 0
    procedure_cache_path: str | None = None
    default_namespace: str = ""
    jwt_secret: str | None = None  # reads JWT_SECRET env
    jwks_url: str | None = None    # reads JWKS_URL env


@dataclass(frozen=True)
class Checkpoint:
    enabled: bool
    db_path: str
    namespace: str
    thread_prefix: str
    backend: str = "sqlite"
    redis_url: str = "redis://localhost:6379/0"
    postgres_dsn: str = ""
    redis_ttl_seconds: int = 86400


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
    digitalocean: DigitalOceanServerless
    openai_compatible: OpenAICompatibleServerless


@dataclass(frozen=True)
class DigitalOceanServerless:
    base_url: str
    api_key: str | None
    timeout_s: int


@dataclass(frozen=True)
class OpenAICompatibleServerless:
    base_url: str
    api_key: str | None
    timeout_s: int


@dataclass(frozen=True)
class VericodingConfig:
    enabled: bool
    extensions: tuple[str, ...]


@dataclass(frozen=True)
class SlaEntry:
    model_id: str
    threshold_p95_s: float
    fallback_model_id: str


@dataclass(frozen=True)
class SlaConfig:
    entries: list[SlaEntry] = field(default_factory=list)


@dataclass(frozen=True)
class AppConfig:
    profile: str
    models: Models
    budgets: Budgets
    policy: Policy
    runner: Runner
    mcp: MCPConfig
    trace: Trace
    remote_api: RemoteAPIConfig
    checkpoint: Checkpoint
    vericoding: VericodingConfig
    audit: AuditConfig = field(default_factory=AuditConfig)
    sla: SlaConfig = field(default_factory=SlaConfig)


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

    env_key_prefix = key.upper()  # "PLANNER" or "ROUTER"
    env_provider = os.environ.get(f"LG_{env_key_prefix}_PROVIDER", "").strip()
    env_model = os.environ.get(f"LG_{env_key_prefix}_MODEL", "").strip()
    if env_provider:
        provider = env_provider
    if env_model:
        model = env_model

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


def _parse_openai_compatible_serverless(models_raw: dict[str, object]) -> OpenAICompatibleServerless:
    section = models_raw.get("openai_compatible")
    section_dict = section if isinstance(section, dict) else {}

    base_url_raw = section_dict.get("base_url", "https://api.openai.com/v1")
    if not isinstance(base_url_raw, str) or not base_url_raw.strip():
        raise ConfigError("missing/invalid models.openai_compatible.base_url")
    base_url = base_url_raw.strip().rstrip("/")
    if not (base_url.startswith("http://") or base_url.startswith("https://")):
        raise ConfigError("models.openai_compatible.base_url must start with http:// or https://")

    api_key_raw = section_dict.get("api_key")
    api_key: str | None
    if api_key_raw is None:
        api_key = (
            os.environ.get("OPENAI_COMPATIBLE_API_KEY")
            or os.environ.get("MODEL_ACCESS_KEY")
        )
    elif isinstance(api_key_raw, str):
        api_key = api_key_raw.strip() or None
    else:
        raise ConfigError("missing/invalid models.openai_compatible.api_key")
    if api_key is not None:
        api_key = api_key.strip() or None

    timeout_raw = section_dict.get("timeout_s", 60)
    if isinstance(timeout_raw, bool) or not isinstance(timeout_raw, int):
        raise ConfigError("missing/invalid models.openai_compatible.timeout_s")
    if timeout_raw < 1:
        raise ConfigError("models.openai_compatible.timeout_s must be >= 1")

    return OpenAICompatibleServerless(base_url=base_url, api_key=api_key, timeout_s=timeout_raw)


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
    remote_api_raw = raw.get("remote_api", {})
    checkpoint_raw = raw.get("checkpoint", {})
    vericoding_raw = raw.get("vericoding", {})
    audit_raw = raw.get("audit", {})
    sla_raw = raw.get("sla", {})
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
    if not isinstance(remote_api_raw, dict):
        raise ConfigError("missing/invalid remote_api")
    if not isinstance(checkpoint_raw, dict):
        raise ConfigError("missing/invalid checkpoint")
    if not isinstance(vericoding_raw, dict):
        raise ConfigError("missing/invalid vericoding")
    if not isinstance(audit_raw, dict):
        raise ConfigError("missing/invalid audit")

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
        openai_compatible=_parse_openai_compatible_serverless(models_raw),
    )

    policy = Policy(
        network_default=_require_str(policy_raw, "network_default"),
        require_approval_for_mutations=_require_bool(policy_raw, "require_approval_for_mutations"),
        allowed_write_paths=_optional_str_tuple(policy_raw, "allowed_write_paths"),
    )
    if policy.network_default not in {"allow", "deny"}:
        raise ConfigError("policy.network_default must be one of: allow, deny")

    api_key_raw = runner_raw.get("api_key")
    if api_key_raw is None:
        api_key_raw = os.environ.get("LG_RUNNER_API_KEY")
    api_key: str | None
    if api_key_raw is None:
        api_key = None
    elif isinstance(api_key_raw, str):
        api_key = api_key_raw.strip() or None  # empty string → unauthenticated
    else:
        raise ConfigError("missing/invalid runner.api_key")

    # Allow LG_RUNNER_BASE_URL to override the configured base_url so that
    # the runner can be an external k8s service without rebuilding the image.
    runner_base_url_env = os.environ.get("LG_RUNNER_BASE_URL", "").strip()
    runner_base_url = runner_base_url_env if runner_base_url_env else _require_str(runner_raw, "base_url")

    runner = Runner(
        base_url=runner_base_url,
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

        schema_hash_raw = server_data.get("schema_hash")
        schema_hash: str | None
        if schema_hash_raw is None:
            schema_hash = None
        elif isinstance(schema_hash_raw, str):
            value = schema_hash_raw.strip().lower()
            if value and not _is_valid_sha256_hex(value):
                raise ConfigError(
                    f"mcp.servers.{server_name}.schema_hash must be a 64-char lowercase hex SHA-256 or absent"
                )
            schema_hash = value or None
        else:
            raise ConfigError(f"missing/invalid mcp.servers.{server_name}.schema_hash")

        servers[server_name.strip()] = MCPServerConfig(
            command=command_raw.strip(),
            args=tuple(args),
            cwd=cwd,
            env=env,
            timeout_s=timeout_s_raw,
            schema_hash=schema_hash,
        )

    mcp = MCPConfig(enabled=mcp_enabled_raw, servers=servers)

    trace = Trace(
        enabled=bool(trace_raw.get("enabled", False)),
        output_dir=str(trace_raw.get("output_dir", "artifacts/runs")),
        capture_model_metadata=bool(trace_raw.get("capture_model_metadata", True)),
    )

    auth_mode_raw = remote_api_raw.get("auth_mode", os.environ.get("LG_REMOTE_API_AUTH_MODE", "off"))
    if not isinstance(auth_mode_raw, str):
        raise ConfigError("missing/invalid remote_api.auth_mode")
    auth_mode = auth_mode_raw.strip().lower() or "off"
    if auth_mode not in {"off", "bearer"}:
        raise ConfigError("remote_api.auth_mode must be one of: off, bearer")

    bearer_token_raw = remote_api_raw.get("bearer_token")
    bearer_token: str | None
    if bearer_token_raw is None:
        bearer_token = os.environ.get("LG_REMOTE_API_BEARER_TOKEN")
    elif isinstance(bearer_token_raw, str):
        bearer_token = bearer_token_raw.strip() or None
    else:
        raise ConfigError("missing/invalid remote_api.bearer_token")
    if bearer_token is not None:
        bearer_token = bearer_token.strip() or None
    if auth_mode == "bearer" and bearer_token is None:
        raise ConfigError("remote_api.bearer_token is required when remote_api.auth_mode=bearer")

    run_store_path_raw = remote_api_raw.get("run_store_path")
    run_store_path: str | None
    if run_store_path_raw is None:
        env_rsp = os.environ.get("LG_REMOTE_API_RUN_STORE_PATH")
        run_store_path = env_rsp.strip() or None if isinstance(env_rsp, str) else None
    elif isinstance(run_store_path_raw, str):
        run_store_path = run_store_path_raw.strip() or None
    else:
        raise ConfigError("missing/invalid remote_api.run_store_path")

    rate_limit_rps_raw = remote_api_raw.get("rate_limit_rps")
    rate_limit_rps: int
    if rate_limit_rps_raw is None:
        env_rlr = os.environ.get("LG_REMOTE_API_RATE_LIMIT_RPS")
        if env_rlr is not None:
            try:
                rate_limit_rps = int(env_rlr.strip())
            except ValueError as exc:
                raise ConfigError("missing/invalid LG_REMOTE_API_RATE_LIMIT_RPS") from exc
        else:
            rate_limit_rps = 0
    else:
        if isinstance(rate_limit_rps_raw, bool) or not isinstance(rate_limit_rps_raw, int):
            raise ConfigError("missing/invalid remote_api.rate_limit_rps")
        rate_limit_rps = rate_limit_rps_raw
    if rate_limit_rps != 0 and rate_limit_rps < 1:
        raise ConfigError("remote_api.rate_limit_rps must be 0 (disabled) or >= 1")

    procedure_cache_path_raw = remote_api_raw.get("procedure_cache_path")
    procedure_cache_path: str | None
    if procedure_cache_path_raw is None:
        env_pcp = os.environ.get("LG_REMOTE_API_PROCEDURE_CACHE_PATH")
        procedure_cache_path = env_pcp.strip() or None if isinstance(env_pcp, str) else None
    elif isinstance(procedure_cache_path_raw, str):
        procedure_cache_path = procedure_cache_path_raw.strip() or None
    else:
        raise ConfigError("missing/invalid remote_api.procedure_cache_path")

    default_namespace_raw = remote_api_raw.get("default_namespace")
    default_namespace: str
    if default_namespace_raw is None:
        env_dn = os.environ.get("LG_REMOTE_API_DEFAULT_NAMESPACE")
        default_namespace = env_dn.strip() if isinstance(env_dn, str) else ""
    elif isinstance(default_namespace_raw, str):
        default_namespace = default_namespace_raw.strip()
    else:
        raise ConfigError("missing/invalid remote_api.default_namespace")
    if default_namespace and not _NAMESPACE_RE.fullmatch(default_namespace):
        raise ConfigError(
            "remote_api.default_namespace must match [A-Za-z0-9_-]{1,64} or be empty"
        )

    jwt_secret_raw = remote_api_raw.get("jwt_secret")
    jwt_secret: str | None
    if jwt_secret_raw is None:
        env_js = os.environ.get("JWT_SECRET")
        jwt_secret = env_js.strip() or None if isinstance(env_js, str) else None
    elif isinstance(jwt_secret_raw, str):
        jwt_secret = jwt_secret_raw.strip() or None
    else:
        raise ConfigError("missing/invalid remote_api.jwt_secret")

    jwks_url_raw = remote_api_raw.get("jwks_url")
    jwks_url: str | None
    if jwks_url_raw is None:
        env_ju = os.environ.get("JWKS_URL")
        jwks_url = env_ju.strip() or None if isinstance(env_ju, str) else None
    elif isinstance(jwks_url_raw, str):
        jwks_url = jwks_url_raw.strip() or None
    else:
        raise ConfigError("missing/invalid remote_api.jwks_url")

    remote_api = RemoteAPIConfig(
        auth_mode=auth_mode,
        bearer_token=bearer_token,
        allow_unauthenticated_healthz=_get_bool(
            remote_api_raw,
            "allow_unauthenticated_healthz",
            default=_env_bool("LG_REMOTE_API_ALLOW_UNAUTHENTICATED_HEALTHZ", default=True),
        ),
        trust_forwarded_headers=_get_bool(
            remote_api_raw,
            "trust_forwarded_headers",
            default=_env_bool("LG_REMOTE_API_TRUST_FORWARDED_HEADERS", default=False),
        ),
        access_log_enabled=_get_bool(
            remote_api_raw,
            "access_log_enabled",
            default=_env_bool("LG_REMOTE_API_ACCESS_LOG_ENABLED", default=True),
        ),
        run_store_path=run_store_path,
        rate_limit_rps=rate_limit_rps,
        procedure_cache_path=procedure_cache_path,
        default_namespace=default_namespace,
        jwt_secret=jwt_secret,
        jwks_url=jwks_url,
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

    checkpoint_backend_raw = checkpoint_raw.get("backend", "sqlite")
    if not isinstance(checkpoint_backend_raw, str):
        raise ConfigError("missing/invalid checkpoint.backend")
    checkpoint_backend = checkpoint_backend_raw.strip().lower() or "sqlite"
    if checkpoint_backend not in {"sqlite", "redis", "postgres"}:
        raise ConfigError("checkpoint.backend must be one of: sqlite, redis, postgres")

    checkpoint_redis_url_raw = checkpoint_raw.get("redis_url", "redis://localhost:6379/0")
    if not isinstance(checkpoint_redis_url_raw, str):
        raise ConfigError("missing/invalid checkpoint.redis_url")
    checkpoint_redis_url = checkpoint_redis_url_raw.strip() or "redis://localhost:6379/0"

    checkpoint_postgres_dsn_raw = checkpoint_raw.get("postgres_dsn", "")
    if not isinstance(checkpoint_postgres_dsn_raw, str):
        raise ConfigError("missing/invalid checkpoint.postgres_dsn")
    checkpoint_postgres_dsn = checkpoint_postgres_dsn_raw.strip()

    checkpoint_redis_ttl_raw = checkpoint_raw.get("redis_ttl_seconds", 86400)
    if isinstance(checkpoint_redis_ttl_raw, bool) or not isinstance(checkpoint_redis_ttl_raw, int):
        raise ConfigError("missing/invalid checkpoint.redis_ttl_seconds")
    if checkpoint_redis_ttl_raw < 1:
        raise ConfigError("checkpoint.redis_ttl_seconds must be >= 1")

    checkpoint = Checkpoint(
        enabled=checkpoint_enabled,
        db_path=checkpoint_db_path_raw.strip(),
        namespace=checkpoint_namespace_raw.strip(),
        thread_prefix=checkpoint_thread_prefix_raw.strip(),
        backend=checkpoint_backend,
        redis_url=checkpoint_redis_url,
        postgres_dsn=checkpoint_postgres_dsn,
        redis_ttl_seconds=checkpoint_redis_ttl_raw,
    )

    vericoding = VericodingConfig(
        enabled=_get_bool(vericoding_raw, "enabled", default=False),
        extensions=_optional_str_tuple(vericoding_raw, "extensions") or (".rs",),
    )

    audit_log_path_raw = audit_raw.get("log_path", "audit.jsonl")
    if not isinstance(audit_log_path_raw, str):
        raise ConfigError("missing/invalid audit.log_path")
    audit_log_path = audit_log_path_raw.strip() or "audit.jsonl"

    audit_sink_type_raw = audit_raw.get("sink_type")
    audit_sink_type: str | None
    if audit_sink_type_raw is None:
        audit_sink_type = None
    elif isinstance(audit_sink_type_raw, str):
        v = audit_sink_type_raw.strip().lower()
        if v and v not in {"s3", "gcs"}:
            raise ConfigError("audit.sink_type must be one of: s3, gcs or absent")
        audit_sink_type = v or None
    else:
        raise ConfigError("missing/invalid audit.sink_type")

    def _optional_str(tbl: dict[str, object], key: str, *, default: str = "") -> str | None:
        raw = tbl.get(key)
        if raw is None:
            return None
        if not isinstance(raw, str):
            raise ConfigError(f"missing/invalid audit.{key}")
        return raw.strip() or None

    s3_bucket = _optional_str(audit_raw, "s3_bucket")
    s3_prefix_raw = audit_raw.get("s3_prefix", "audit")
    s3_prefix = s3_prefix_raw.strip() if isinstance(s3_prefix_raw, str) else "audit"
    s3_region_raw = audit_raw.get("s3_region", "us-east-1")
    s3_region = s3_region_raw.strip() if isinstance(s3_region_raw, str) else "us-east-1"
    gcs_bucket = _optional_str(audit_raw, "gcs_bucket")
    gcs_prefix_raw = audit_raw.get("gcs_prefix", "audit")
    gcs_prefix = gcs_prefix_raw.strip() if isinstance(gcs_prefix_raw, str) else "audit"

    audit = AuditConfig(
        log_path=audit_log_path,
        sink_type=audit_sink_type,
        s3_bucket=s3_bucket,
        s3_prefix=s3_prefix or "audit",
        s3_region=s3_region or "us-east-1",
        gcs_bucket=gcs_bucket,
        gcs_prefix=gcs_prefix or "audit",
    )

    # SLA config
    sla_entries: list[SlaEntry] = []
    if isinstance(sla_raw, dict):
        entries_raw = sla_raw.get("entries", [])
        if not isinstance(entries_raw, list):
            raise ConfigError("missing/invalid sla.entries")
        for idx, entry_raw in enumerate(entries_raw):
            if not isinstance(entry_raw, dict):
                raise ConfigError(f"missing/invalid sla.entries[{idx}]")
            model_id_raw = entry_raw.get("model_id")
            if not isinstance(model_id_raw, str) or not model_id_raw.strip():
                raise ConfigError(f"missing/invalid sla.entries[{idx}].model_id")
            threshold_raw = entry_raw.get("threshold_p95_s")
            threshold_p95_s = _parse_float(threshold_raw, default=-1.0)
            if threshold_p95_s < 0.0:
                raise ConfigError(f"missing/invalid sla.entries[{idx}].threshold_p95_s")
            fallback_raw = entry_raw.get("fallback_model_id")
            if not isinstance(fallback_raw, str) or not fallback_raw.strip():
                raise ConfigError(f"missing/invalid sla.entries[{idx}].fallback_model_id")
            sla_entries.append(
                SlaEntry(
                    model_id=model_id_raw.strip(),
                    threshold_p95_s=threshold_p95_s,
                    fallback_model_id=fallback_raw.strip(),
                )
            )
    sla = SlaConfig(entries=sla_entries)

    return AppConfig(
        profile=profile,
        models=models,
        budgets=budgets,
        policy=policy,
        runner=runner,
        mcp=mcp,
        trace=trace,
        remote_api=remote_api,
        checkpoint=checkpoint,
        vericoding=vericoding,
        audit=audit,
        sla=sla,
    )
