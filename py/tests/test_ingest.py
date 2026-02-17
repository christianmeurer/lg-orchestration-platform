from __future__ import annotations

from typing import Any

from lg_orch.nodes.ingest import ingest


def _base_state(**overrides: Any) -> dict[str, Any]:
    s: dict[str, Any] = {
        "request": "fix the bug",
        "_repo_root": ".",
        "_runner_base_url": "http://127.0.0.1:8088",
        "_budget_max_loops": 3,
        "_config_policy": {"network_default": "deny", "require_approval_for_mutations": True},
    }
    s.update(overrides)
    return s


def test_ingest_preserves_request() -> None:
    out = ingest(_base_state(request="hello world"))
    assert out["request"] == "hello world"


def test_ingest_strips_whitespace() -> None:
    out = ingest(_base_state(request="  padded  "))
    assert out["request"] == "padded"


def test_ingest_empty_request_defaults_to_empty() -> None:
    out = ingest(_base_state(request=""))
    assert out["request"] == ""


def test_ingest_generates_run_id() -> None:
    out = ingest(_base_state())
    assert "_run_id" in out
    assert isinstance(out["_run_id"], str)
    assert len(out["_run_id"]) == 32  # uuid4 hex


def test_ingest_preserves_existing_run_id() -> None:
    out = ingest(_base_state(_run_id="existing123"))
    assert out["_run_id"] == "existing123"


def test_ingest_preserves_internal_keys() -> None:
    out = ingest(_base_state(_custom_key="value"))
    assert out["_custom_key"] == "value"


def test_ingest_drops_non_internal_extra_keys() -> None:
    out = ingest(_base_state(extra_key="should_vanish"))
    assert "extra_key" not in out


def test_ingest_creates_trace_event() -> None:
    out = ingest(_base_state())
    events = out.get("_trace_events", [])
    assert len(events) >= 1
    last = events[-1]
    assert last["kind"] == "node"
    assert last["data"]["name"] == "ingest"
    assert last["data"]["phase"] == "end"


def test_ingest_initializes_orch_state_fields() -> None:
    out = ingest(_base_state())
    assert out["intent"] == "analysis"
    assert out["repo_context"] == {}
    assert out["facts"] == []
    assert out["plan"] is None
    assert out["tool_results"] == []
    assert out["final"] == ""
