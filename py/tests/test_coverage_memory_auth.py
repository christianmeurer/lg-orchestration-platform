"""Tests for memory.py helpers and auth.py coverage gaps."""
from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from lg_orch.memory import (
    _as_int,
    _normalize_history_policy,
    _provenance,
    _state_get,
    _state_to_dict,
    _tool_results,
    approx_token_count,
)


# ---------------------------------------------------------------------------
# _state_get
# ---------------------------------------------------------------------------


class SampleModel(BaseModel):
    name: str = "test"

    class Config:
        extra = "allow"


def test_state_get_dict() -> None:
    assert _state_get({"key": "val"}, "key") == "val"


def test_state_get_dict_default() -> None:
    assert _state_get({}, "key", "default") == "default"


def test_state_get_pydantic() -> None:
    m = SampleModel(name="hello")
    assert _state_get(m, "name") == "hello"


def test_state_get_pydantic_extra() -> None:
    m = SampleModel(name="hello", extra_field="extra")  # type: ignore[call-arg]
    assert _state_get(m, "extra_field") == "extra"


def test_state_get_unknown_type() -> None:
    assert _state_get("not_a_dict_or_model", "key", "default") == "default"


# ---------------------------------------------------------------------------
# _state_to_dict
# ---------------------------------------------------------------------------


def test_state_to_dict_from_dict() -> None:
    d = {"a": 1}
    result = _state_to_dict(d)
    assert result == {"a": 1}
    assert result is not d  # should be a copy


def test_state_to_dict_from_pydantic() -> None:
    m = SampleModel(name="hello")
    result = _state_to_dict(m)
    assert result["name"] == "hello"


def test_state_to_dict_from_pydantic_with_extra() -> None:
    m = SampleModel(name="hello", _custom="val")  # type: ignore[call-arg]
    result = _state_to_dict(m)
    assert "_custom" in result


def test_state_to_dict_unknown_type() -> None:
    assert _state_to_dict("not_a_model") == {}


# ---------------------------------------------------------------------------
# _as_int (memory version with min/max)
# ---------------------------------------------------------------------------


def test_as_int_bool_returns_default() -> None:
    assert _as_int(True, default=5, minimum=0, maximum=100) == 5


def test_as_int_int_clamped() -> None:
    assert _as_int(200, default=5, minimum=0, maximum=100) == 100
    assert _as_int(-5, default=5, minimum=0, maximum=100) == 0


def test_as_int_float() -> None:
    assert _as_int(3.7, default=5, minimum=0, maximum=100) == 3


def test_as_int_string() -> None:
    assert _as_int(" 42 ", default=5, minimum=0, maximum=100) == 42


def test_as_int_empty_string() -> None:
    assert _as_int("", default=5, minimum=0, maximum=100) == 5


def test_as_int_bad_string() -> None:
    assert _as_int("abc", default=5, minimum=0, maximum=100) == 5


def test_as_int_none() -> None:
    assert _as_int(None, default=5, minimum=0, maximum=100) == 5


# ---------------------------------------------------------------------------
# _normalize_history_policy
# ---------------------------------------------------------------------------


def test_normalize_history_policy_defaults() -> None:
    result = _normalize_history_policy({})
    assert "schema_version" in result
    assert result["retain_recent_tool_results"] >= 5
    assert result["read_file_prune_threshold_chars"] >= 200


def test_normalize_history_policy_custom_values() -> None:
    result = _normalize_history_policy({
        "retain_recent_tool_results": 50,
        "read_file_prune_threshold_chars": 5000,
    })
    assert result["retain_recent_tool_results"] == 50
    assert result["read_file_prune_threshold_chars"] == 5000


def test_normalize_history_policy_clamps_low() -> None:
    result = _normalize_history_policy({
        "retain_recent_tool_results": 1,
        "read_file_prune_threshold_chars": 10,
    })
    assert result["retain_recent_tool_results"] >= 5
    assert result["read_file_prune_threshold_chars"] >= 200


# ---------------------------------------------------------------------------
# _tool_results / _provenance
# ---------------------------------------------------------------------------


def test_tool_results_from_dict() -> None:
    state = {"tool_results": [{"tool": "exec", "ok": True}, {"tool": "read"}]}
    assert len(_tool_results(state)) == 2


def test_tool_results_non_list() -> None:
    assert _tool_results({"tool_results": "not_a_list"}) == []


def test_tool_results_filters_non_dicts() -> None:
    assert len(_tool_results({"tool_results": [{"a": 1}, "bad", 42]})) == 1


def test_provenance_from_dict() -> None:
    state = {"provenance": [{"source": "file.py"}]}
    assert len(_provenance(state)) == 1


def test_provenance_non_list() -> None:
    assert _provenance({"provenance": "not_a_list"}) == []


# ---------------------------------------------------------------------------
# approx_token_count
# ---------------------------------------------------------------------------


def test_approx_token_count_empty() -> None:
    assert approx_token_count("") == 0


def test_approx_token_count_short() -> None:
    count = approx_token_count("hello world this is a test")
    assert count > 0


def test_approx_token_count_longer() -> None:
    text = "word " * 1000
    count = approx_token_count(text)
    assert count > 100


# ---------------------------------------------------------------------------
# auth.py: _route_policy coverage
# ---------------------------------------------------------------------------


from lg_orch.auth import (
    AuthError,
    JWTSettings,
    TokenClaims,
    _check_roles,
    _extract_bearer_token,
    _route_policy,
    authorize_stdlib,
    jwt_settings_from_config,
)


def test_route_policy_healthz() -> None:
    assert _route_policy(route="/healthz", method="GET", path_parts=["healthz"], jwt_enabled=True) == ()


def test_route_policy_root() -> None:
    assert _route_policy(route="/", method="GET", path_parts=[], jwt_enabled=True) == ()


def test_route_policy_ui() -> None:
    assert _route_policy(route="/ui", method="GET", path_parts=["ui"], jwt_enabled=True) == ()


def test_route_policy_metrics_jwt_disabled() -> None:
    assert _route_policy(route="/metrics", method="GET", path_parts=["metrics"], jwt_enabled=False) == ()


def test_route_policy_metrics_jwt_enabled() -> None:
    roles = _route_policy(route="/metrics", method="GET", path_parts=["metrics"], jwt_enabled=True)
    assert "admin" in roles


def test_route_policy_app() -> None:
    assert _route_policy(route="/app/index.html", method="GET", path_parts=["app", "index.html"], jwt_enabled=True) == ()


def test_route_policy_post_v1_runs() -> None:
    roles = _route_policy(route="/v1/runs", method="POST", path_parts=["v1", "runs"], jwt_enabled=True)
    assert "operator" in roles


def test_route_policy_get_v1_runs() -> None:
    roles = _route_policy(route="/v1/runs", method="GET", path_parts=["v1", "runs"], jwt_enabled=True)
    assert "viewer" in roles


def test_route_policy_runs_search() -> None:
    roles = _route_policy(route="/runs/search", method="GET", path_parts=["runs", "search"], jwt_enabled=True)
    assert "viewer" in roles


def test_route_policy_get_v1_run_detail() -> None:
    roles = _route_policy(route="/v1/runs/abc", method="GET", path_parts=["v1", "runs", "abc"], jwt_enabled=True)
    assert "viewer" in roles


def test_route_policy_get_runs_detail() -> None:
    roles = _route_policy(route="/runs/abc", method="GET", path_parts=["runs", "abc"], jwt_enabled=True)
    assert "viewer" in roles


def test_route_policy_get_runs_stream() -> None:
    roles = _route_policy(route="/runs/abc/stream", method="GET", path_parts=["runs", "abc", "stream"], jwt_enabled=True)
    assert "viewer" in roles


def test_route_policy_logs() -> None:
    roles = _route_policy(route="/v1/runs/abc/logs", method="GET", path_parts=["v1", "runs", "abc", "logs"], jwt_enabled=True)
    assert "viewer" in roles


def test_route_policy_approve() -> None:
    roles = _route_policy(route="/v1/runs/abc/approve", method="POST", path_parts=["v1", "runs", "abc", "approve"], jwt_enabled=True)
    assert "operator" in roles


def test_route_policy_vote() -> None:
    roles = _route_policy(route="/runs/abc/vote", method="POST", path_parts=["runs", "abc", "vote"], jwt_enabled=True)
    assert "operator" in roles


def test_route_policy_approval_policy() -> None:
    roles = _route_policy(route="/runs/abc/approval-policy", method="POST", path_parts=["runs", "abc", "approval-policy"], jwt_enabled=True)
    assert "admin" in roles


def test_route_policy_delete_runs() -> None:
    roles = _route_policy(route="/runs/abc", method="DELETE", path_parts=["runs", "abc"], jwt_enabled=True)
    assert "admin" in roles


def test_route_policy_delete_v1_runs() -> None:
    roles = _route_policy(route="/v1/runs/abc", method="DELETE", path_parts=["v1", "runs", "abc"], jwt_enabled=False)
    # even with jwt_enabled=False, DELETE requires admin
    assert "admin" in roles


def test_route_policy_unknown_route() -> None:
    roles = _route_policy(route="/other", method="GET", path_parts=["other"], jwt_enabled=True)
    # Should require auth (viewer/operator/admin)
    assert len(roles) > 0


# ---------------------------------------------------------------------------
# _extract_bearer_token
# ---------------------------------------------------------------------------


def test_extract_bearer_token_valid() -> None:
    assert _extract_bearer_token("Bearer my-token") == "my-token"


def test_extract_bearer_token_missing() -> None:
    with pytest.raises(AuthError):
        _extract_bearer_token(None)


def test_extract_bearer_token_no_bearer_prefix() -> None:
    with pytest.raises(AuthError):
        _extract_bearer_token("Basic abc")


def test_extract_bearer_token_empty_token() -> None:
    with pytest.raises(AuthError):
        _extract_bearer_token("Bearer  ")


# ---------------------------------------------------------------------------
# _check_roles
# ---------------------------------------------------------------------------


def test_check_roles_sufficient() -> None:
    claims = TokenClaims(sub="user", roles=["admin"], exp=0, iat=0)
    _check_roles(claims, ("admin",))  # should not raise


def test_check_roles_insufficient() -> None:
    claims = TokenClaims(sub="user", roles=["viewer"], exp=0, iat=0)
    with pytest.raises(AuthError) as exc_info:
        _check_roles(claims, ("admin",))
    assert exc_info.value.status_code == 403


def test_check_roles_empty_required() -> None:
    claims = TokenClaims(sub="user", roles=[], exp=0, iat=0)
    _check_roles(claims, ())  # should not raise


# ---------------------------------------------------------------------------
# authorize_stdlib
# ---------------------------------------------------------------------------


def test_authorize_stdlib_disabled() -> None:
    settings = JWTSettings(jwt_secret=None, jwks_url=None)
    claims = authorize_stdlib(authorization=None, settings=settings)
    assert claims.sub == "anonymous"
    assert claims.roles == []


# ---------------------------------------------------------------------------
# jwt_settings_from_config
# ---------------------------------------------------------------------------


def test_jwt_settings_from_config() -> None:
    settings = jwt_settings_from_config(jwt_secret="s3cr3t", jwks_url=None)
    assert settings.enabled is True
    assert settings.jwt_secret == "s3cr3t"


def test_jwt_settings_disabled() -> None:
    settings = jwt_settings_from_config(jwt_secret=None, jwks_url=None)
    assert settings.enabled is False
