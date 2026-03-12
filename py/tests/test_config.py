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
    Policy,
    Runner,
    Trace,
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
        checkpoint=Checkpoint(
            enabled=True,
            db_path="artifacts/checkpoints/langgraph.sqlite",
            namespace="main",
            thread_prefix="lg-orch",
        ),
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
