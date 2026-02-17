from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from lg_orch.config import AppConfig, Budgets, Policy, Runner, Trace, load_config

_VALID_TOML = """\
[models.router]
provider = "local"
model = "deterministic"
temperature = 0.0

[models.planner]
provider = "local"
model = "deterministic"
temperature = 0.0

[budgets]
max_loops = 5
max_tool_calls_per_loop = 10
max_patch_bytes = 100000
tool_timeout_s = 300

[policy]
network_default = "deny"
require_approval_for_mutations = true

[runner]
base_url = "http://127.0.0.1:9090"
root_dir = "/tmp/test"

[trace]
enabled = true
output_dir = "out/traces"
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


def test_load_config_parses_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LG_PROFILE", "dev")
    with tempfile.TemporaryDirectory() as td:
        root = _write_config(td)
        cfg = load_config(repo_root=root)
        assert cfg.runner.base_url == "http://127.0.0.1:9090"
        assert cfg.runner.root_dir == "/tmp/test"


def test_load_config_parses_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LG_PROFILE", "dev")
    with tempfile.TemporaryDirectory() as td:
        root = _write_config(td)
        cfg = load_config(repo_root=root)
        assert cfg.trace.enabled is True
        assert cfg.trace.output_dir == "out/traces"


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
"""
    monkeypatch.setenv("LG_PROFILE", "dev")
    with tempfile.TemporaryDirectory() as td:
        root = _write_config(td, content=toml_no_trace)
        cfg = load_config(repo_root=root)
        assert cfg.trace.enabled is False
        assert cfg.trace.output_dir == "artifacts/runs"


def test_dataclasses_are_frozen() -> None:
    b = Budgets(max_loops=1, max_tool_calls_per_loop=1, max_patch_bytes=1, tool_timeout_s=1)
    with pytest.raises(AttributeError):
        b.max_loops = 99  # type: ignore[misc]


def test_appconfig_frozen() -> None:
    cfg = AppConfig(
        profile="dev",
        budgets=Budgets(
            max_loops=1, max_tool_calls_per_loop=1, max_patch_bytes=1, tool_timeout_s=1
        ),
        policy=Policy(network_default="deny", require_approval_for_mutations=True),
        runner=Runner(base_url="http://localhost:8088", root_dir=".", api_key=None),
        trace=Trace(enabled=False, output_dir="out"),
    )
    with pytest.raises(AttributeError):
        cfg.profile = "prod"  # type: ignore[misc]
