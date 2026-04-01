"""Tests for config.py helper functions and edge cases to boost coverage."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from lg_orch.config import (
    ConfigError,
    _env_bool,
    _get_bool,
    _get_int,
    _is_valid_sha256_hex,
    _opt_bool,
    _opt_int,
    _opt_int_or_env,
    _opt_str,
    _opt_str_or_env,
    _optional_str_tuple,
    _parse_float,
    _parse_model_endpoint,
    _parse_model_routing,
    _require,
    _require_bool,
    _require_int,
    _require_section_int,
    _require_section_str,
    _require_str,
    load_config,
)


# ---------------------------------------------------------------------------
# _is_valid_sha256_hex
# ---------------------------------------------------------------------------


def test_is_valid_sha256_hex_valid() -> None:
    assert _is_valid_sha256_hex("a" * 64) is True
    assert _is_valid_sha256_hex("0123456789abcdef" * 4) is True


def test_is_valid_sha256_hex_invalid() -> None:
    assert _is_valid_sha256_hex("a" * 63) is False
    assert _is_valid_sha256_hex("g" * 64) is False
    assert _is_valid_sha256_hex("") is False


# ---------------------------------------------------------------------------
# _require_str
# ---------------------------------------------------------------------------


def test_require_str_missing_key() -> None:
    with pytest.raises(ConfigError):
        _require_str({}, "name")


def test_require_str_empty_string() -> None:
    with pytest.raises(ConfigError):
        _require_str({"name": "  "}, "name")


def test_require_str_non_string() -> None:
    with pytest.raises(ConfigError):
        _require_str({"name": 42}, "name")


# ---------------------------------------------------------------------------
# _require_int
# ---------------------------------------------------------------------------


def test_require_int_with_bool_raises() -> None:
    with pytest.raises(ConfigError):
        _require_int({"val": True}, "val")


def test_require_int_with_none_raises() -> None:
    with pytest.raises(ConfigError):
        _require_int({}, "val")


def test_require_int_with_float() -> None:
    assert _require_int({"val": 3.7}, "val") == 3


def test_require_int_with_string() -> None:
    assert _require_int({"val": " 42 "}, "val") == 42


def test_require_int_with_bad_string_raises() -> None:
    with pytest.raises(ConfigError):
        _require_int({"val": "abc"}, "val")


def test_require_int_with_list_raises() -> None:
    with pytest.raises(ConfigError):
        _require_int({"val": [1, 2]}, "val")


# ---------------------------------------------------------------------------
# _require_bool
# ---------------------------------------------------------------------------


def test_require_bool_non_bool_raises() -> None:
    with pytest.raises(ConfigError):
        _require_bool({"val": "true"}, "val")


def test_require_bool_missing_raises() -> None:
    with pytest.raises(ConfigError):
        _require_bool({}, "val")


# ---------------------------------------------------------------------------
# _get_int / _get_bool
# ---------------------------------------------------------------------------


def test_get_int_default() -> None:
    assert _get_int({}, "x", default=99) == 99


def test_get_int_present() -> None:
    assert _get_int({"x": 7}, "x", default=99) == 7


def test_get_bool_default() -> None:
    assert _get_bool({}, "x", default=True) is True


def test_get_bool_present() -> None:
    assert _get_bool({"x": False}, "x", default=True) is False


# ---------------------------------------------------------------------------
# _require_section_str / _require_section_int
# ---------------------------------------------------------------------------


def test_require_section_str_missing() -> None:
    with pytest.raises(ConfigError, match="missing required"):
        _require({}, "key", "section")


def test_require_section_str_non_string() -> None:
    with pytest.raises(ConfigError, match="non-empty string"):
        _require_section_str({"key": 42}, "key", "section")


def test_require_section_str_empty() -> None:
    with pytest.raises(ConfigError, match="non-empty string"):
        _require_section_str({"key": "  "}, "key", "section")


def test_require_section_str_valid() -> None:
    assert _require_section_str({"key": " hello "}, "key", "section") == "hello"


def test_require_section_int_bool_raises() -> None:
    with pytest.raises(ConfigError, match="integer"):
        _require_section_int({"key": True}, "key", "section")


def test_require_section_int_string() -> None:
    assert _require_section_int({"key": " 42 "}, "key", "section") == 42


def test_require_section_int_bad_string_raises() -> None:
    with pytest.raises(ConfigError, match="integer"):
        _require_section_int({"key": "abc"}, "key", "section")


# ---------------------------------------------------------------------------
# _opt_str / _opt_int / _opt_bool
# ---------------------------------------------------------------------------


def test_opt_str_absent_returns_default() -> None:
    assert _opt_str({}, "key", default="fallback") == "fallback"


def test_opt_str_non_string_raises() -> None:
    with pytest.raises(ConfigError, match="must be a string"):
        _opt_str({"key": 42}, "key")


def test_opt_str_strips() -> None:
    assert _opt_str({"key": " hello "}, "key") == "hello"


def test_opt_int_absent_returns_default() -> None:
    assert _opt_int({}, "key", default=10) == 10


def test_opt_int_bool_raises() -> None:
    with pytest.raises(ConfigError, match="must be an integer"):
        _opt_int({"key": True}, "key")


def test_opt_int_string_value() -> None:
    assert _opt_int({"key": " 7 "}, "key") == 7


def test_opt_int_bad_string_raises() -> None:
    with pytest.raises(ConfigError, match="must be an integer"):
        _opt_int({"key": "abc"}, "key")


def test_opt_bool_absent_returns_default() -> None:
    assert _opt_bool({}, "key", default=True) is True


def test_opt_bool_non_bool_raises() -> None:
    with pytest.raises(ConfigError, match="must be a boolean"):
        _opt_bool({"key": "yes"}, "key")


# ---------------------------------------------------------------------------
# _opt_str_or_env / _opt_int_or_env
# ---------------------------------------------------------------------------


def test_opt_str_or_env_from_config() -> None:
    assert _opt_str_or_env({"key": "value"}, "key", "UNUSED_ENV") == "value"


def test_opt_str_or_env_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_VAR", "env_value")
    assert _opt_str_or_env({}, "key", "TEST_VAR") == "env_value"


def test_opt_str_or_env_none_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEST_VAR", raising=False)
    assert _opt_str_or_env({}, "key", "TEST_VAR") is None


def test_opt_str_or_env_non_string_raises() -> None:
    with pytest.raises(ConfigError):
        _opt_str_or_env({"key": 42}, "key", "UNUSED_ENV")


def test_opt_str_or_env_empty_string_returns_none() -> None:
    assert _opt_str_or_env({"key": "  "}, "key", "UNUSED_ENV") is None


def test_opt_int_or_env_from_config() -> None:
    assert _opt_int_or_env({"key": 42}, "key", "UNUSED_ENV") == 42


def test_opt_int_or_env_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_VAR", "99")
    assert _opt_int_or_env({}, "key", "TEST_VAR") == 99


def test_opt_int_or_env_bad_env_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_VAR", "abc")
    with pytest.raises(ConfigError):
        _opt_int_or_env({}, "key", "TEST_VAR")


def test_opt_int_or_env_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEST_VAR", raising=False)
    assert _opt_int_or_env({}, "key", "TEST_VAR", default=5) == 5


def test_opt_int_or_env_bool_raises() -> None:
    with pytest.raises(ConfigError):
        _opt_int_or_env({"key": True}, "key", "UNUSED_ENV")


# ---------------------------------------------------------------------------
# _env_bool
# ---------------------------------------------------------------------------


def test_env_bool_true_values(monkeypatch: pytest.MonkeyPatch) -> None:
    for val in ("1", "true", "yes", "on", "TRUE", "YES"):
        monkeypatch.setenv("MY_FLAG", val)
        assert _env_bool("MY_FLAG", default=False) is True


def test_env_bool_false_values(monkeypatch: pytest.MonkeyPatch) -> None:
    for val in ("0", "false", "no", "off", "FALSE", "NO"):
        monkeypatch.setenv("MY_FLAG", val)
        assert _env_bool("MY_FLAG", default=True) is False


def test_env_bool_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MY_FLAG", raising=False)
    assert _env_bool("MY_FLAG", default=True) is True
    assert _env_bool("MY_FLAG", default=False) is False


def test_env_bool_invalid_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_FLAG", "maybe")
    with pytest.raises(ConfigError):
        _env_bool("MY_FLAG", default=False)


# ---------------------------------------------------------------------------
# _optional_str_tuple
# ---------------------------------------------------------------------------


def test_optional_str_tuple_none_returns_empty() -> None:
    assert _optional_str_tuple({"paths": None}, "paths") == ()


def test_optional_str_tuple_non_list_raises() -> None:
    with pytest.raises(ConfigError):
        _optional_str_tuple({"paths": "not_a_list"}, "paths")


def test_optional_str_tuple_empty_entry_raises() -> None:
    with pytest.raises(ConfigError):
        _optional_str_tuple({"paths": ["valid", "  "]}, "paths")


def test_optional_str_tuple_non_string_entry_raises() -> None:
    with pytest.raises(ConfigError):
        _optional_str_tuple({"paths": [42]}, "paths")


def test_optional_str_tuple_strips() -> None:
    assert _optional_str_tuple({"paths": [" a ", " b "]}, "paths") == ("a", "b")


# ---------------------------------------------------------------------------
# _parse_float
# ---------------------------------------------------------------------------


def test_parse_float_bool_returns_default() -> None:
    assert _parse_float(True, default=0.5) == 0.5


def test_parse_float_int() -> None:
    assert _parse_float(3, default=0.0) == 3.0


def test_parse_float_string() -> None:
    assert _parse_float(" 1.5 ", default=0.0) == 1.5


def test_parse_float_bad_string_returns_default() -> None:
    assert _parse_float("abc", default=0.5) == 0.5


def test_parse_float_none_returns_default() -> None:
    assert _parse_float(None, default=0.5) == 0.5


# ---------------------------------------------------------------------------
# _parse_model_endpoint
# ---------------------------------------------------------------------------


def test_parse_model_endpoint_missing_section_returns_defaults() -> None:
    ep = _parse_model_endpoint({}, "router")
    assert ep.provider == "local"
    assert ep.model == "deterministic"
    assert ep.temperature == 0.0


def test_parse_model_endpoint_non_dict_returns_defaults() -> None:
    ep = _parse_model_endpoint({"router": "not_a_dict"}, "router")
    assert ep.provider == "local"


def test_parse_model_endpoint_empty_provider_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LG_ROUTER_PROVIDER", raising=False)
    monkeypatch.delenv("LG_ROUTER_MODEL", raising=False)
    ep = _parse_model_endpoint(
        {"router": {"provider": "", "model": "", "temperature": 0.0}}, "router"
    )
    assert ep.provider == "local"
    assert ep.model == "deterministic"


def test_parse_model_endpoint_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LG_ROUTER_PROVIDER", "openai")
    monkeypatch.setenv("LG_ROUTER_MODEL", "gpt-4")
    ep = _parse_model_endpoint(
        {"router": {"provider": "local", "model": "deterministic", "temperature": 0.0}}, "router"
    )
    assert ep.provider == "openai"
    assert ep.model == "gpt-4"


# ---------------------------------------------------------------------------
# _parse_model_routing
# ---------------------------------------------------------------------------


def test_parse_model_routing_missing_section_returns_defaults() -> None:
    routing = _parse_model_routing({})
    assert routing.local_provider == "local"
    assert routing.interactive_context_limit == 1800


def test_parse_model_routing_invalid_interactive_limit_raises() -> None:
    with pytest.raises(ConfigError):
        _parse_model_routing(
            {
                "routing": {
                    "local_provider": "local",
                    "interactive_context_limit": 0,
                }
            }
        )


def test_parse_model_routing_deep_lt_interactive_raises() -> None:
    with pytest.raises(ConfigError):
        _parse_model_routing(
            {
                "routing": {
                    "local_provider": "local",
                    "interactive_context_limit": 2000,
                    "deep_planning_context_limit": 1000,
                }
            }
        )


def test_parse_model_routing_negative_recovery_threshold_raises() -> None:
    with pytest.raises(ConfigError):
        _parse_model_routing(
            {
                "routing": {
                    "local_provider": "local",
                    "recovery_retry_threshold": -1,
                }
            }
        )


def test_parse_model_routing_empty_local_provider_falls_back() -> None:
    routing = _parse_model_routing({"routing": {"local_provider": ""}})
    assert routing.local_provider == "local"


def test_parse_model_routing_empty_cache_affinity_falls_back() -> None:
    routing = _parse_model_routing({"routing": {"local_provider": "local", "default_cache_affinity": ""}})
    assert routing.default_cache_affinity == "workspace"


def test_parse_model_routing_empty_fallback_classes_defaults() -> None:
    routing = _parse_model_routing({"routing": {"local_provider": "local", "fallback_task_classes": []}})
    assert "summarization" in routing.fallback_task_classes


# ---------------------------------------------------------------------------
# load_config edge cases
# ---------------------------------------------------------------------------


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
    cfg_path = cfg_dir / f"runtime.{profile}.toml"
    cfg_path.write_text(content, encoding="utf-8")
    return root


def test_load_config_budgets_max_loops_zero_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    toml = _MINIMAL_TOML.replace("max_loops = 5", "max_loops = 0")
    monkeypatch.setenv("LG_PROFILE", "dev")
    with tempfile.TemporaryDirectory() as td:
        root = _write_config(td, "dev", toml)
        with pytest.raises(ConfigError, match="max_loops"):
            load_config(repo_root=root)


def test_load_config_budgets_patch_bytes_zero_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    toml = _MINIMAL_TOML.replace("max_patch_bytes = 100000", "max_patch_bytes = 0")
    monkeypatch.setenv("LG_PROFILE", "dev")
    with tempfile.TemporaryDirectory() as td:
        root = _write_config(td, "dev", toml)
        with pytest.raises(ConfigError, match="max_patch_bytes"):
            load_config(repo_root=root)


def test_load_config_budgets_tool_timeout_zero_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    toml = _MINIMAL_TOML.replace("tool_timeout_s = 300", "tool_timeout_s = 0")
    monkeypatch.setenv("LG_PROFILE", "dev")
    with tempfile.TemporaryDirectory() as td:
        root = _write_config(td, "dev", toml)
        with pytest.raises(ConfigError, match="tool_timeout_s"):
            load_config(repo_root=root)


def test_load_config_policy_bad_network_default_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    toml = _MINIMAL_TOML.replace('network_default = "deny"', 'network_default = "maybe"')
    monkeypatch.setenv("LG_PROFILE", "dev")
    with tempfile.TemporaryDirectory() as td:
        root = _write_config(td, "dev", toml)
        with pytest.raises(ConfigError, match="network_default"):
            load_config(repo_root=root)


def test_load_config_runner_bad_url_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    toml = _MINIMAL_TOML.replace('base_url = "http://127.0.0.1:9090"', 'base_url = "ftp://nope"')
    monkeypatch.setenv("LG_PROFILE", "dev")
    with tempfile.TemporaryDirectory() as td:
        root = _write_config(td, "dev", toml)
        with pytest.raises(ConfigError, match="base_url"):
            load_config(repo_root=root)


def test_load_config_invalid_toml_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LG_PROFILE", "dev")
    with tempfile.TemporaryDirectory() as td:
        root = _write_config(td, "dev", "this is not valid toml [[[")
        with pytest.raises(ConfigError, match="invalid toml"):
            load_config(repo_root=root)


def test_load_config_sla_with_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    toml = _MINIMAL_TOML + """
[trace]
enabled = false

[remote_api]
auth_mode = "off"

[[sla.entries]]
model_id = "gpt-4"
threshold_p95_s = 5.0
fallback_model_id = "gpt-3.5-turbo"
"""
    monkeypatch.setenv("LG_PROFILE", "dev")
    with tempfile.TemporaryDirectory() as td:
        root = _write_config(td, "dev", toml)
        cfg = load_config(repo_root=root)
        assert len(cfg.sla.entries) == 1
        assert cfg.sla.entries[0].model_id == "gpt-4"
        assert cfg.sla.entries[0].threshold_p95_s == 5.0
        assert cfg.sla.entries[0].fallback_model_id == "gpt-3.5-turbo"


def test_load_config_sla_bad_threshold_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    toml = _MINIMAL_TOML + """
[[sla.entries]]
model_id = "gpt-4"
threshold_p95_s = "not_a_number"
fallback_model_id = "gpt-3.5-turbo"
"""
    monkeypatch.setenv("LG_PROFILE", "dev")
    with tempfile.TemporaryDirectory() as td:
        root = _write_config(td, "dev", toml)
        with pytest.raises(ConfigError, match="threshold_p95_s"):
            load_config(repo_root=root)


def test_load_config_sla_missing_model_id_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    toml = _MINIMAL_TOML + """
[[sla.entries]]
threshold_p95_s = 5.0
fallback_model_id = "gpt-3.5-turbo"
"""
    monkeypatch.setenv("LG_PROFILE", "dev")
    with tempfile.TemporaryDirectory() as td:
        root = _write_config(td, "dev", toml)
        with pytest.raises(ConfigError, match="model_id"):
            load_config(repo_root=root)


def test_load_config_bad_auth_mode_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    toml = _MINIMAL_TOML + """
[remote_api]
auth_mode = "oauth"
"""
    monkeypatch.setenv("LG_PROFILE", "dev")
    with tempfile.TemporaryDirectory() as td:
        root = _write_config(td, "dev", toml)
        with pytest.raises(ConfigError, match="auth_mode"):
            load_config(repo_root=root)


def test_load_config_bearer_without_token_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    toml = _MINIMAL_TOML + """
[remote_api]
auth_mode = "bearer"
"""
    monkeypatch.setenv("LG_PROFILE", "dev")
    monkeypatch.delenv("LG_REMOTE_API_BEARER_TOKEN", raising=False)
    with tempfile.TemporaryDirectory() as td:
        root = _write_config(td, "dev", toml)
        with pytest.raises(ConfigError, match="bearer_token"):
            load_config(repo_root=root)


def test_load_config_bad_namespace_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    toml = _MINIMAL_TOML + """
[remote_api]
auth_mode = "off"
default_namespace = "invalid namespace with spaces!!"
"""
    monkeypatch.setenv("LG_PROFILE", "dev")
    with tempfile.TemporaryDirectory() as td:
        root = _write_config(td, "dev", toml)
        with pytest.raises(ConfigError, match="default_namespace"):
            load_config(repo_root=root)


def test_load_config_bad_checkpoint_backend_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    toml = _MINIMAL_TOML + """
[checkpoint]
backend = "mysql"
"""
    monkeypatch.setenv("LG_PROFILE", "dev")
    with tempfile.TemporaryDirectory() as td:
        root = _write_config(td, "dev", toml)
        with pytest.raises(ConfigError, match="checkpoint.backend"):
            load_config(repo_root=root)


def test_load_config_bad_audit_sink_type_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    toml = _MINIMAL_TOML + """
[audit]
sink_type = "azure"
"""
    monkeypatch.setenv("LG_PROFILE", "dev")
    with tempfile.TemporaryDirectory() as td:
        root = _write_config(td, "dev", toml)
        with pytest.raises(ConfigError, match="sink_type"):
            load_config(repo_root=root)
