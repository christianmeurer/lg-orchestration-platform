from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from lg_orch.config import (
    AppConfig,
    Budgets,
    Checkpoint,
    DigitalOceanServerless,
    MCPConfig,
    MCPServerConfig,
    ModelEndpoint,
    ModelRouting,
    Models,
    OpenAICompatibleServerless,
    Policy,
    RemoteAPIConfig,
    Runner,
    Trace,
    VericodingConfig,
    load_config,
)

_VALID_TOML = """\
[models.router]
provider = "local"
model = "deterministic"
temperature = 0.0

[models.planner]
provider = "local"
model = "deterministic"
temperature = 0.0

[models.routing]
local_provider = "local"
fallback_task_classes = ["summarization", "lint_reflection", "context_condensation"]

[models.digitalocean]
base_url = "https://inference.do-ai.run/v1"
timeout_s = 45

[budgets]
max_loops = 5
max_tool_calls_per_loop = 10
max_patch_bytes = 100000
tool_timeout_s = 300

[policy]
network_default = "deny"
require_approval_for_mutations = true
allowed_write_paths = ["py/**", "docs/**"]

[runner]
base_url = "http://127.0.0.1:9090"
root_dir = "/tmp/test"

[mcp]
enabled = true

[mcp.servers.mock]
command = "python"
args = ["server.py"]
cwd = "."
timeout_s = 30

[mcp.servers.mock.env]
MODE = "test"

[trace]
enabled = true
output_dir = "out/traces"

[remote_api]
auth_mode = "bearer"
bearer_token = "remote-token"
allow_unauthenticated_healthz = true
trust_forwarded_headers = true
access_log_enabled = true

[checkpoint]
enabled = true
db_path = "artifacts/checkpoints/langgraph.sqlite"
namespace = "main"
thread_prefix = "lg-orch"
"""


def _write_config(tmpdir: str, profile: str = "dev", content: str = _VALID_TOML) -> Path:
    root = Path(tmpdir)
    cfg_dir = root / "configs"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = cfg_dir / f"runtime.{profile}.toml"
    cfg_path.write_text(content, encoding="utf-8")
    return root


def test_load_config_parses_budgets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LG_PROFILE", "dev")
    with tempfile.TemporaryDirectory() as td:
        root = _write_config(td)
        cfg = load_config(repo_root=root)
        assert cfg.budgets.max_loops == 5
        assert cfg.budgets.max_tool_calls_per_loop == 10
        assert cfg.budgets.max_patch_bytes == 100000
        assert cfg.budgets.tool_timeout_s == 300


def test_load_config_parses_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LG_PROFILE", "dev")
    with tempfile.TemporaryDirectory() as td:
        root = _write_config(td)
        cfg = load_config(repo_root=root)
        assert cfg.policy.network_default == "deny"
        assert cfg.policy.require_approval_for_mutations is True
        assert cfg.policy.allowed_write_paths == ("py/**", "docs/**")


def test_load_config_parses_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LG_PROFILE", "dev")
    with tempfile.TemporaryDirectory() as td:
        root = _write_config(td)
        cfg = load_config(repo_root=root)
        assert cfg.runner.base_url == "http://127.0.0.1:9090"
        assert cfg.runner.root_dir == "/tmp/test"


def test_load_config_parses_mcp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LG_PROFILE", "dev")
    with tempfile.TemporaryDirectory() as td:
        root = _write_config(td)
        cfg = load_config(repo_root=root)
        assert cfg.mcp.enabled is True
        assert "mock" in cfg.mcp.servers
        mock = cfg.mcp.servers["mock"]
        assert mock.command == "python"
        assert mock.args == ("server.py",)
        assert mock.cwd == "."
        assert mock.timeout_s == 30
        assert mock.env["MODE"] == "test"


def test_load_config_parses_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LG_PROFILE", "dev")
    with tempfile.TemporaryDirectory() as td:
        root = _write_config(td)
        cfg = load_config(repo_root=root)
        assert cfg.trace.enabled is True
        assert cfg.trace.output_dir == "out/traces"


def test_load_config_parses_remote_api(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LG_PROFILE", "dev")
    with tempfile.TemporaryDirectory() as td:
        root = _write_config(td)
        cfg = load_config(repo_root=root)
        assert cfg.remote_api.auth_mode == "bearer"
        assert cfg.remote_api.bearer_token == "remote-token"
        assert cfg.remote_api.allow_unauthenticated_healthz is True
        assert cfg.remote_api.trust_forwarded_headers is True
        assert cfg.remote_api.access_log_enabled is True


def test_load_config_parses_checkpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LG_PROFILE", "dev")
    with tempfile.TemporaryDirectory() as td:
        root = _write_config(td)
        cfg = load_config(repo_root=root)
        assert cfg.checkpoint.enabled is True
        assert cfg.checkpoint.db_path == "artifacts/checkpoints/langgraph.sqlite"
        assert cfg.checkpoint.namespace == "main"
        assert cfg.checkpoint.thread_prefix == "lg-orch"


def test_load_config_parses_models_and_routing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LG_PROFILE", "dev")
    with tempfile.TemporaryDirectory() as td:
        root = _write_config(td)
        cfg = load_config(repo_root=root)
        assert cfg.models.router.provider == "local"
        assert cfg.models.router.model == "deterministic"
        assert cfg.models.planner.provider == "local"
        assert cfg.models.routing.local_provider == "local"
        assert "summarization" in cfg.models.routing.fallback_task_classes
        assert cfg.models.digitalocean.base_url == "https://inference.do-ai.run/v1"
        assert cfg.models.digitalocean.timeout_s == 45


def test_load_config_uses_profile_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LG_PROFILE", "stage")
    with tempfile.TemporaryDirectory() as td:
        root = _write_config(td, profile="stage")
        cfg = load_config(repo_root=root)
        assert cfg.profile == "stage"


def test_load_config_defaults_to_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LG_PROFILE", raising=False)
    with tempfile.TemporaryDirectory() as td:
        root = _write_config(td, profile="dev")
        cfg = load_config(repo_root=root)
        assert cfg.profile == "dev"


def test_load_config_missing_file_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LG_PROFILE", "nonexistent")
    with tempfile.TemporaryDirectory() as td, pytest.raises(FileNotFoundError):
        load_config(repo_root=Path(td))


def test_load_config_trace_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    toml_no_trace = """\
[models.router]
provider = "local"
model = "deterministic"
temperature = 0.0

[models.planner]
provider = "local"
model = "deterministic"
temperature = 0.0

[budgets]
max_loops = 1
max_tool_calls_per_loop = 1
max_patch_bytes = 1
tool_timeout_s = 1

[policy]
network_default = "deny"
require_approval_for_mutations = false

[runner]
base_url = "http://localhost:8088"
root_dir = "."

[mcp]
enabled = false
"""
    monkeypatch.setenv("LG_PROFILE", "dev")
    with tempfile.TemporaryDirectory() as td:
        root = _write_config(td, content=toml_no_trace)
        cfg = load_config(repo_root=root)
        assert cfg.trace.enabled is False
        assert cfg.trace.output_dir == "artifacts/runs"
        assert cfg.checkpoint.enabled is True
        assert cfg.checkpoint.db_path == "artifacts/checkpoints/langgraph.sqlite"
        assert cfg.checkpoint.namespace == "main"
        assert cfg.checkpoint.thread_prefix == "lg-orch"
        assert cfg.models.routing.local_provider == "local"
        assert "summarization" in cfg.models.routing.fallback_task_classes
        assert cfg.remote_api.auth_mode == "off"
        assert cfg.remote_api.bearer_token is None
        assert cfg.mcp.enabled is False
        assert cfg.mcp.servers == {}


def test_dataclasses_are_frozen() -> None:
    b = Budgets(max_loops=1, max_tool_calls_per_loop=1, max_patch_bytes=1, tool_timeout_s=1)
    with pytest.raises(AttributeError):
        b.max_loops = 99  # type: ignore[misc]


def test_appconfig_frozen() -> None:
    cfg = AppConfig(
        profile="dev",
        models=Models(
            router=ModelEndpoint(provider="local", model="deterministic", temperature=0.0),
            planner=ModelEndpoint(provider="local", model="deterministic", temperature=0.0),
            routing=ModelRouting(
                local_provider="local",
                fallback_task_classes=("summarization",),
            ),
            digitalocean=DigitalOceanServerless(
                base_url="https://inference.do-ai.run/v1",
                api_key=None,
                timeout_s=60,
            ),
            openai_compatible=OpenAICompatibleServerless(
                base_url="https://api.openai.com/v1",
                api_key=None,
                timeout_s=60,
            ),
        ),
        budgets=Budgets(
            max_loops=1, max_tool_calls_per_loop=1, max_patch_bytes=1, tool_timeout_s=1
        ),
        policy=Policy(network_default="deny", require_approval_for_mutations=True),
        runner=Runner(base_url="http://localhost:8088", root_dir=".", api_key=None),
        mcp=MCPConfig(
            enabled=False,
            servers={
                "mock": MCPServerConfig(
                    command="python",
                    args=("server.py",),
                    cwd=None,
                    env={},
                    timeout_s=20,
                )
            },
        ),
        trace=Trace(enabled=False, output_dir="out"),
        remote_api=RemoteAPIConfig(auth_mode="off", bearer_token=None),
        checkpoint=Checkpoint(
            enabled=True,
            db_path="artifacts/checkpoints/langgraph.sqlite",
            namespace="main",
            thread_prefix="lg-orch",
        ),
        vericoding=VericodingConfig(enabled=True, extensions=(".rs",)),
    )
    with pytest.raises(AttributeError):
        cfg.profile = "prod"  # type: ignore[misc]


def test_load_config_mcp_invalid_timeout_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    bad_toml = (
        _VALID_TOML
        + "\n[mcp]\nenabled = true\n[mcp.servers.s1]\n"
        + "command = \"python\"\ntimeout_s = 0\n"
    )
    monkeypatch.setenv("LG_PROFILE", "dev")
    with tempfile.TemporaryDirectory() as td:
        root = _write_config(td, content=bad_toml)
        with pytest.raises(ValueError):
            load_config(repo_root=root)


def test_load_config_digitalocean_api_key_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LG_PROFILE", "dev")
    monkeypatch.setenv("MODEL_ACCESS_KEY", "do-model-key")
    with tempfile.TemporaryDirectory() as td:
        root = _write_config(td)
        cfg = load_config(repo_root=root)
        assert cfg.models.digitalocean.api_key == "do-model-key"


def test_load_config_digitalocean_timeout_must_be_positive(monkeypatch: pytest.MonkeyPatch) -> None:
    bad_toml = _VALID_TOML.replace("timeout_s = 45", "timeout_s = 0")
    monkeypatch.setenv("LG_PROFILE", "dev")
    with tempfile.TemporaryDirectory() as td:
        root = _write_config(td, content=bad_toml)
        with pytest.raises(ValueError):
            load_config(repo_root=root)


def test_load_config_run_store_path_none_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LG_PROFILE", "dev")
    monkeypatch.delenv("LG_REMOTE_API_RUN_STORE_PATH", raising=False)
    with tempfile.TemporaryDirectory() as td:
        root = _write_config(td)
        cfg = load_config(repo_root=root)
        assert cfg.remote_api.run_store_path is None


def test_load_config_run_store_path_from_toml(monkeypatch: pytest.MonkeyPatch) -> None:
    toml = _VALID_TOML.replace(
        "[remote_api]\nauth_mode = \"bearer\"\nbearer_token = \"remote-token\"\n"
        "allow_unauthenticated_healthz = true\ntrust_forwarded_headers = true\n"
        "access_log_enabled = true",
        "[remote_api]\nauth_mode = \"bearer\"\nbearer_token = \"remote-token\"\n"
        "allow_unauthenticated_healthz = true\ntrust_forwarded_headers = true\n"
        "access_log_enabled = true\nrun_store_path = \"artifacts/runs.sqlite\"",
    )
    monkeypatch.setenv("LG_PROFILE", "dev")
    with tempfile.TemporaryDirectory() as td:
        root = _write_config(td, content=toml)
        cfg = load_config(repo_root=root)
        assert cfg.remote_api.run_store_path == "artifacts/runs.sqlite"


def test_load_config_run_store_path_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LG_PROFILE", "dev")
    monkeypatch.setenv("LG_REMOTE_API_RUN_STORE_PATH", "artifacts/env-runs.sqlite")
    with tempfile.TemporaryDirectory() as td:
        root = _write_config(td)
        cfg = load_config(repo_root=root)
        assert cfg.remote_api.run_store_path == "artifacts/env-runs.sqlite"


def test_load_config_run_store_path_empty_string_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    toml = _VALID_TOML.replace(
        "[remote_api]\nauth_mode = \"bearer\"\nbearer_token = \"remote-token\"\n"
        "allow_unauthenticated_healthz = true\ntrust_forwarded_headers = true\n"
        "access_log_enabled = true",
        "[remote_api]\nauth_mode = \"bearer\"\nbearer_token = \"remote-token\"\n"
        "allow_unauthenticated_healthz = true\ntrust_forwarded_headers = true\n"
        "access_log_enabled = true\nrun_store_path = \"\"",
    )
    monkeypatch.setenv("LG_PROFILE", "dev")
    monkeypatch.delenv("LG_REMOTE_API_RUN_STORE_PATH", raising=False)
    with tempfile.TemporaryDirectory() as td:
        root = _write_config(td, content=toml)
        cfg = load_config(repo_root=root)
        assert cfg.remote_api.run_store_path is None


def test_load_config_openai_compatible_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LG_PROFILE", "dev")
    monkeypatch.delenv("OPENAI_COMPATIBLE_API_KEY", raising=False)
    monkeypatch.delenv("MODEL_ACCESS_KEY", raising=False)
    with tempfile.TemporaryDirectory() as td:
        root = _write_config(td)
        cfg = load_config(repo_root=root)
        assert cfg.models.openai_compatible.base_url == "https://api.openai.com/v1"
        assert cfg.models.openai_compatible.api_key is None
        assert cfg.models.openai_compatible.timeout_s == 60


def test_load_config_openai_compatible_from_toml(monkeypatch: pytest.MonkeyPatch) -> None:
    extra = (
        "\n[models.openai_compatible]\n"
        "base_url = \"https://my-endpoint.example.com/v1\"\n"
        "timeout_s = 30\n"
    )
    monkeypatch.setenv("LG_PROFILE", "dev")
    with tempfile.TemporaryDirectory() as td:
        root = _write_config(td, content=_VALID_TOML + extra)
        cfg = load_config(repo_root=root)
        assert cfg.models.openai_compatible.base_url == "https://my-endpoint.example.com/v1"
        assert cfg.models.openai_compatible.timeout_s == 30


def test_load_config_openai_compatible_api_key_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LG_PROFILE", "dev")
    monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "oc-key-xyz")
    monkeypatch.delenv("MODEL_ACCESS_KEY", raising=False)
    with tempfile.TemporaryDirectory() as td:
        root = _write_config(td)
        cfg = load_config(repo_root=root)
        assert cfg.models.openai_compatible.api_key == "oc-key-xyz"


def test_load_config_openai_compatible_falls_back_to_model_access_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LG_PROFILE", "dev")
    monkeypatch.delenv("OPENAI_COMPATIBLE_API_KEY", raising=False)
    monkeypatch.setenv("MODEL_ACCESS_KEY", "fallback-key")
    with tempfile.TemporaryDirectory() as td:
        root = _write_config(td)
        cfg = load_config(repo_root=root)
        assert cfg.models.openai_compatible.api_key == "fallback-key"


# ---------------------------------------------------------------------------
# schema_hash parsing
# ---------------------------------------------------------------------------

_VALID_HASH = "a" * 64  # 64 lowercase hex chars


def test_mcp_server_config_schema_hash_absent_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LG_PROFILE", "dev")
    with tempfile.TemporaryDirectory() as td:
        root = _write_config(td)
        cfg = load_config(repo_root=root)
        assert cfg.mcp.servers["mock"].schema_hash is None


def test_mcp_server_config_schema_hash_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LG_PROFILE", "dev")
    toml = _VALID_TOML + f'\nschema_hash = "{_VALID_HASH}"\n'
    # Insert schema_hash under [mcp.servers.mock]
    toml_with_hash = _VALID_TOML.replace(
        "[mcp.servers.mock.env]",
        f'schema_hash = "{_VALID_HASH}"\n\n[mcp.servers.mock.env]',
    )
    with tempfile.TemporaryDirectory() as td:
        root = _write_config(td, content=toml_with_hash)
        cfg = load_config(repo_root=root)
        assert cfg.mcp.servers["mock"].schema_hash == _VALID_HASH


def test_procedure_cache_path_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LG_PROFILE", "dev")
    toml = _VALID_TOML.replace(
        "access_log_enabled = true\n",
        'access_log_enabled = true\nprocedure_cache_path = "artifacts/remote-api/procedures.sqlite"\n',
        1,
    )
    with tempfile.TemporaryDirectory() as td:
        root = _write_config(td, content=toml)
        cfg = load_config(repo_root=root)
        assert cfg.remote_api.procedure_cache_path == "artifacts/remote-api/procedures.sqlite"


def test_procedure_cache_path_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LG_PROFILE", "dev")
    monkeypatch.setenv("LG_REMOTE_API_PROCEDURE_CACHE_PATH", "artifacts/env-procedures.sqlite")
    with tempfile.TemporaryDirectory() as td:
        root = _write_config(td, content=_VALID_TOML)
        cfg = load_config(repo_root=root)
        assert cfg.remote_api.procedure_cache_path == "artifacts/env-procedures.sqlite"


def test_mcp_server_config_schema_hash_invalid_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from lg_orch.config import ConfigError

    monkeypatch.setenv("LG_PROFILE", "dev")
    toml_bad_hash = _VALID_TOML.replace(
        "[mcp.servers.mock.env]",
        'schema_hash = "not-a-valid-hash"\n\n[mcp.servers.mock.env]',
    )
    with tempfile.TemporaryDirectory() as td:
        root = _write_config(td, content=toml_bad_hash)
        with pytest.raises(ConfigError, match="schema_hash"):
            load_config(repo_root=root)


# ---------------------------------------------------------------------------
# default_namespace tests
# ---------------------------------------------------------------------------


def test_default_namespace_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LG_PROFILE", "dev")
    toml = _VALID_TOML.replace(
        "[checkpoint]",
        'default_namespace = "tenant-1"\n\n[checkpoint]',
    )
    with tempfile.TemporaryDirectory() as td:
        root = _write_config(td, content=toml)
        cfg = load_config(repo_root=root)
        assert cfg.remote_api.default_namespace == "tenant-1"


def test_default_namespace_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LG_PROFILE", "dev")
    monkeypatch.setenv("LG_REMOTE_API_DEFAULT_NAMESPACE", "env-ns")
    with tempfile.TemporaryDirectory() as td:
        root = _write_config(td, content=_VALID_TOML)
        cfg = load_config(repo_root=root)
        assert cfg.remote_api.default_namespace == "env-ns"


def test_default_namespace_invalid_chars_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from lg_orch.config import ConfigError

    monkeypatch.setenv("LG_PROFILE", "dev")
    toml = _VALID_TOML.replace(
        "[checkpoint]",
        'default_namespace = "invalid namespace!"\n\n[checkpoint]',
    )
    with tempfile.TemporaryDirectory() as td:
        root = _write_config(td, content=toml)
        with pytest.raises(ConfigError, match="default_namespace"):
            load_config(repo_root=root)
