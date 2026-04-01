"""Tests for lg_orch.nodes._utils helpers."""

from __future__ import annotations

import pytest

from lg_orch.nodes._utils import extract_json_block, resolve_inference_client, validate_base_url

# ---------------------------------------------------------------------------
# validate_base_url
# ---------------------------------------------------------------------------


def test_validate_base_url_accepts_http() -> None:
    validate_base_url("http://localhost:8080")


def test_validate_base_url_accepts_https() -> None:
    validate_base_url("https://api.example.com/v1")


def test_validate_base_url_rejects_ftp() -> None:
    with pytest.raises(ValueError, match="http://"):
        validate_base_url("ftp://example.com")


def test_validate_base_url_rejects_empty() -> None:
    with pytest.raises(ValueError):
        validate_base_url("")


def test_validate_base_url_uses_label() -> None:
    with pytest.raises(ValueError, match="my_url"):
        validate_base_url("badscheme://x", label="my_url")


# ---------------------------------------------------------------------------
# extract_json_block
# ---------------------------------------------------------------------------


def test_extract_json_block_fenced_json() -> None:
    text = 'Some text\n```json\n{"key": "value"}\n```\nMore text'
    assert extract_json_block(text) == '{"key": "value"}'


def test_extract_json_block_fenced_no_lang() -> None:
    text = 'prefix\n```\n{"a": 1}\n```\nsuffix'
    assert extract_json_block(text) == '{"a": 1}'


def test_extract_json_block_raw_object() -> None:
    text = 'The answer is {"result": 42} ok'
    assert extract_json_block(text) == '{"result": 42}'


def test_extract_json_block_raw_array() -> None:
    text = "Here is the list: [1, 2, 3] done"
    assert extract_json_block(text) == "[1, 2, 3]"


def test_extract_json_block_returns_none_for_no_json() -> None:
    assert extract_json_block("no json here") is None


def test_extract_json_block_prefers_fenced_over_raw() -> None:
    text = '{"outer": 1}\n```json\n{"inner": 2}\n```'
    result = extract_json_block(text)
    assert result == '{"inner": 2}'


# ---------------------------------------------------------------------------
# resolve_inference_client — error paths
# ---------------------------------------------------------------------------


def test_resolve_inference_client_raises_for_local_provider() -> None:
    state = {"_models": {"planner": {"provider": "local", "model": "det"}}}
    with pytest.raises(ValueError, match="local"):
        resolve_inference_client(state, "planner", "digitalocean")


def test_resolve_inference_client_raises_for_empty_model() -> None:
    state = {"_models": {"planner": {"provider": "remote", "model": ""}}}
    with pytest.raises(ValueError, match="empty"):
        resolve_inference_client(state, "planner", "digitalocean")


def test_resolve_inference_client_raises_for_missing_do_api_key() -> None:
    state = {
        "_models": {"planner": {"provider": "digitalocean", "model": "gpt-4.1"}},
        "_model_provider_runtime": {
            "digitalocean": {"api_key": "", "base_url": "https://x.com/v1"}
        },
    }
    with pytest.raises(ValueError, match="api_key"):
        resolve_inference_client(state, "planner", "digitalocean")


def test_resolve_inference_client_raises_for_missing_openai_api_key() -> None:
    state = {
        "_models": {"planner": {"provider": "openai_compatible", "model": "gpt-4.1"}},
        "_model_provider_runtime": {
            "openai_compatible": {"api_key": "", "base_url": "https://api.openai.com/v1"}
        },
    }
    with pytest.raises(ValueError, match="api_key"):
        resolve_inference_client(state, "planner", "digitalocean")


def test_resolve_inference_client_raises_for_empty_do_base_url() -> None:
    state = {
        "_models": {"planner": {"provider": "digitalocean", "model": "gpt-4.1"}},
        "_model_provider_runtime": {"digitalocean": {"api_key": "sk-test", "base_url": ""}},
    }
    with pytest.raises(ValueError, match="base_url"):
        resolve_inference_client(state, "planner", "digitalocean")


def test_resolve_inference_client_raises_for_empty_openai_base_url() -> None:
    state = {
        "_models": {"planner": {"provider": "openai_compatible", "model": "gpt-4.1"}},
        "_model_provider_runtime": {"openai_compatible": {"api_key": "sk-test", "base_url": ""}},
    }
    with pytest.raises(ValueError, match="base_url"):
        resolve_inference_client(state, "planner", "digitalocean")


def test_resolve_inference_client_success_digitalocean() -> None:
    state = {
        "_models": {"planner": {"provider": "digitalocean", "model": "gpt-4.1"}},
        "_model_provider_runtime": {
            "digitalocean": {"api_key": "sk-test", "base_url": "https://inference.do-ai.run/v1"}
        },
    }
    client, model = resolve_inference_client(state, "planner", "digitalocean")
    assert model == "gpt-4.1"
    client.close()


def test_resolve_inference_client_success_openai_compatible() -> None:
    state = {
        "_models": {"planner": {"provider": "openai_compatible", "model": "gpt-4o"}},
        "_model_provider_runtime": {
            "openai_compatible": {"api_key": "sk-test", "base_url": "https://api.openai.com/v1"}
        },
    }
    client, model = resolve_inference_client(state, "planner", "digitalocean")
    assert model == "gpt-4o"
    client.close()
