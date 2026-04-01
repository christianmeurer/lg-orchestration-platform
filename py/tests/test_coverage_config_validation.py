"""Tests for load_config validation branches that remain uncovered."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from lg_orch.config import (
    ConfigError,
    _parse_digitalocean_serverless,
    _parse_openai_compatible_serverless,
    load_config,
)

_MINIMAL_TOML = """\
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
require_approval_for_mutations = false

[runner]
base_url = "http://127.0.0.1:9090"
root_dir = "."

[mcp]
enabled = false
"""


def _write_config(tmpdir: str, profile: str, content: str) -> Path:
    root = Path(tmpdir)
    cfg_dir = root / "configs"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / f"runtime.{profile}.toml").write_text(content, encoding="utf-8")
    return root


# ---------------------------------------------------------------------------
# _parse_digitalocean_serverless
# ---------------------------------------------------------------------------


def test_parse_do_missing_section_uses_defaults() -> None:
    result = _parse_digitalocean_serverless({})
    assert result.base_url.startswith("https://")
    assert result.timeout_s > 0


def test_parse_do_bad_base_url_raises() -> None:
    with pytest.raises(ConfigError, match="base_url"):
        _parse_digitalocean_serverless({"digitalocean": {"base_url": "ftp://nope"}})


def test_parse_do_empty_base_url_raises() -> None:
    with pytest.raises(ConfigError, match="base_url"):
        _parse_digitalocean_serverless({"digitalocean": {"base_url": "  "}})


def test_parse_do_bad_api_key_type_raises() -> None:
    with pytest.raises(ConfigError, match="api_key"):
        _parse_digitalocean_serverless({"digitalocean": {"api_key": 123}})


def test_parse_do_api_key_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_ACCESS_KEY", "env-key")
    result = _parse_digitalocean_serverless({"digitalocean": {}})
    assert result.api_key == "env-key"


def test_parse_do_api_key_empty_string_is_none() -> None:
    result = _parse_digitalocean_serverless({"digitalocean": {"api_key": "  "}})
    assert result.api_key is None


def test_parse_do_bad_timeout_type_raises() -> None:
    with pytest.raises(ConfigError, match="timeout_s"):
        _parse_digitalocean_serverless({"digitalocean": {"timeout_s": True}})


def test_parse_do_zero_timeout_raises() -> None:
    with pytest.raises(ConfigError, match="timeout_s"):
        _parse_digitalocean_serverless({"digitalocean": {"timeout_s": 0}})


# ---------------------------------------------------------------------------
# _parse_openai_compatible_serverless
# ---------------------------------------------------------------------------


def test_parse_oai_missing_section_uses_defaults() -> None:
    result = _parse_openai_compatible_serverless({})
    assert result.base_url.startswith("https://")


def test_parse_oai_bad_base_url_raises() -> None:
    with pytest.raises(ConfigError, match="base_url"):
        _parse_openai_compatible_serverless({"openai_compatible": {"base_url": "ftp://nope"}})


def test_parse_oai_bad_api_key_type_raises() -> None:
    with pytest.raises(ConfigError, match="api_key"):
        _parse_openai_compatible_serverless({"openai_compatible": {"api_key": 123}})


def test_parse_oai_api_key_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "oai-key")
    monkeypatch.delenv("MODEL_ACCESS_KEY", raising=False)
    result = _parse_openai_compatible_serverless({"openai_compatible": {}})
    assert result.api_key == "oai-key"


def test_parse_oai_bad_timeout_raises() -> None:
    with pytest.raises(ConfigError, match="timeout_s"):
        _parse_openai_compatible_serverless({"openai_compatible": {"timeout_s": True}})


def test_parse_oai_zero_timeout_raises() -> None:
    with pytest.raises(ConfigError, match="timeout_s"):
        _parse_openai_compatible_serverless({"openai_compatible": {"timeout_s": 0}})


# ---------------------------------------------------------------------------
# load_config: section type checks (lines 586-604)
# ---------------------------------------------------------------------------


def test_load_config_max_tool_calls_negative_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    toml = _MINIMAL_TOML.replace(
        "max_tool_calls_per_loop = 10",
        "max_tool_calls_per_loop = -1",
    )
    monkeypatch.setenv("LG_PROFILE", "dev")
    with tempfile.TemporaryDirectory() as td:
        root = _write_config(td, "dev", toml)
        with pytest.raises(ConfigError, match="max_tool_calls_per_loop"):
            load_config(repo_root=root)


def test_load_config_stable_prefix_tokens_too_low_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    toml = _MINIMAL_TOML + "\n[budgets]\nstable_prefix_tokens = 0\n"
    # This won't work because TOML doesn't allow duplicate sections.
    # Instead, we need to put it in the existing budgets section.
    toml = _MINIMAL_TOML.replace(
        "tool_timeout_s = 300",
        "tool_timeout_s = 300\nstable_prefix_tokens = 0",
    )
    monkeypatch.setenv("LG_PROFILE", "dev")
    with tempfile.TemporaryDirectory() as td:
        root = _write_config(td, "dev", toml)
        with pytest.raises(ConfigError, match="stable_prefix_tokens"):
            load_config(repo_root=root)


def test_load_config_working_set_tokens_too_low_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    toml = _MINIMAL_TOML.replace(
        "tool_timeout_s = 300",
        "tool_timeout_s = 300\nworking_set_tokens = 0",
    )
    monkeypatch.setenv("LG_PROFILE", "dev")
    with tempfile.TemporaryDirectory() as td:
        root = _write_config(td, "dev", toml)
        with pytest.raises(ConfigError, match="working_set_tokens"):
            load_config(repo_root=root)


def test_load_config_tool_result_summary_chars_too_low_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    toml = _MINIMAL_TOML.replace(
        "tool_timeout_s = 300",
        "tool_timeout_s = 300\ntool_result_summary_chars = 10",
    )
    monkeypatch.setenv("LG_PROFILE", "dev")
    with tempfile.TemporaryDirectory() as td:
        root = _write_config(td, "dev", toml)
        with pytest.raises(ConfigError, match="tool_result_summary_chars"):
            load_config(repo_root=root)


def test_load_config_mcp_server_bad_timeout_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    toml = _MINIMAL_TOML.replace("enabled = false", "enabled = true") + """
[mcp.servers.bad]
command = "echo"
timeout_s = 0
"""
    monkeypatch.setenv("LG_PROFILE", "dev")
    with tempfile.TemporaryDirectory() as td:
        root = _write_config(td, "dev", toml)
        with pytest.raises(ConfigError, match="timeout_s"):
            load_config(repo_root=root)


def test_load_config_mcp_server_bad_command_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    toml = _MINIMAL_TOML.replace("enabled = false", "enabled = true") + """
[mcp.servers.bad]
command = ""
"""
    monkeypatch.setenv("LG_PROFILE", "dev")
    with tempfile.TemporaryDirectory() as td:
        root = _write_config(td, "dev", toml)
        with pytest.raises(ConfigError, match="command"):
            load_config(repo_root=root)


def test_load_config_mcp_server_bad_args_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    toml = _MINIMAL_TOML.replace("enabled = false", "enabled = true") + """
[mcp.servers.bad]
command = "echo"
args = "not_a_list"
"""
    monkeypatch.setenv("LG_PROFILE", "dev")
    with tempfile.TemporaryDirectory() as td:
        root = _write_config(td, "dev", toml)
        with pytest.raises(ConfigError, match="args"):
            load_config(repo_root=root)


def test_load_config_mcp_server_bad_schema_hash_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    toml = _MINIMAL_TOML.replace("enabled = false", "enabled = true") + """
[mcp.servers.bad]
command = "echo"
schema_hash = "not-a-valid-sha256"
"""
    monkeypatch.setenv("LG_PROFILE", "dev")
    with tempfile.TemporaryDirectory() as td:
        root = _write_config(td, "dev", toml)
        with pytest.raises(ConfigError, match="schema_hash"):
            load_config(repo_root=root)


def test_load_config_mcp_server_valid_schema_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    valid_sha = "a" * 64
    toml = _MINIMAL_TOML.replace("enabled = false", "enabled = true") + f"""
[mcp.servers.good]
command = "echo"
schema_hash = "{valid_sha}"
"""
    monkeypatch.setenv("LG_PROFILE", "dev")
    with tempfile.TemporaryDirectory() as td:
        root = _write_config(td, "dev", toml)
        cfg = load_config(repo_root=root)
        assert cfg.mcp.servers["good"].schema_hash == valid_sha


def test_load_config_rate_limit_rps_negative_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    toml = _MINIMAL_TOML + """
[remote_api]
auth_mode = "off"
rate_limit_rps = -1
"""
    monkeypatch.setenv("LG_PROFILE", "dev")
    with tempfile.TemporaryDirectory() as td:
        root = _write_config(td, "dev", toml)
        # -1 is not valid: must be 0 or >= 1.  But -1 < 0 so _opt_int_or_env
        # should accept it as int, then validation catches it.
        # Actually the validation is: if != 0 and < 1 then error
        # -1 != 0 and -1 < 1 -> raises
        with pytest.raises(ConfigError, match="rate_limit_rps"):
            load_config(repo_root=root)


def test_load_config_checkpoint_redis_ttl_zero_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    toml = _MINIMAL_TOML + """
[checkpoint]
redis_ttl_seconds = 0
"""
    monkeypatch.setenv("LG_PROFILE", "dev")
    with tempfile.TemporaryDirectory() as td:
        root = _write_config(td, "dev", toml)
        with pytest.raises(ConfigError, match="redis_ttl_seconds"):
            load_config(repo_root=root)


def test_load_config_runner_api_key_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LG_PROFILE", "dev")
    monkeypatch.setenv("LG_RUNNER_API_KEY", "secret-from-env")
    with tempfile.TemporaryDirectory() as td:
        root = _write_config(td, "dev", _MINIMAL_TOML)
        cfg = load_config(repo_root=root)
        assert cfg.runner.api_key == "secret-from-env"


def test_load_config_runner_base_url_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LG_PROFILE", "dev")
    monkeypatch.setenv("LG_RUNNER_BASE_URL", "http://custom:9090")
    with tempfile.TemporaryDirectory() as td:
        root = _write_config(td, "dev", _MINIMAL_TOML)
        cfg = load_config(repo_root=root)
        assert cfg.runner.base_url == "http://custom:9090"


def test_load_config_mcp_server_cwd_non_string_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    toml = _MINIMAL_TOML.replace("enabled = false", "enabled = true") + """
[mcp.servers.bad]
command = "echo"
cwd = 123
"""
    monkeypatch.setenv("LG_PROFILE", "dev")
    with tempfile.TemporaryDirectory() as td:
        root = _write_config(td, "dev", toml)
        with pytest.raises(ConfigError, match="cwd"):
            load_config(repo_root=root)


def test_load_config_mcp_server_env_non_dict_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    toml = _MINIMAL_TOML.replace("enabled = false", "enabled = true") + """
[mcp.servers.bad]
command = "echo"
env = "not_a_dict"
"""
    monkeypatch.setenv("LG_PROFILE", "dev")
    with tempfile.TemporaryDirectory() as td:
        root = _write_config(td, "dev", toml)
        with pytest.raises(ConfigError, match="env"):
            load_config(repo_root=root)
