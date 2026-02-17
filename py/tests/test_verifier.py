from __future__ import annotations

from typing import Any

from lg_orch.nodes.verifier import verifier


def _base_state(**overrides: Any) -> dict[str, Any]:
    s: dict[str, Any] = {"request": "test", "tool_results": []}
    s.update(overrides)
    return s


def test_verifier_creates_ok_report() -> None:
    out = verifier(_base_state())
    assert out["verification"]["ok"] is True
    assert out["verification"]["checks"] == []


def test_verifier_creates_trace_events() -> None:
    out = verifier(_base_state())
    events = out.get("_trace_events", [])
    names = [e["data"]["name"] for e in events if e["kind"] == "node"]
    assert "verifier" in names


def test_verifier_trace_has_start_and_end() -> None:
    out = verifier(_base_state())
    events = out.get("_trace_events", [])
    verifier_events = [e for e in events if e["kind"] == "node" and e["data"]["name"] == "verifier"]
    phases = [e["data"]["phase"] for e in verifier_events]
    assert "start" in phases
    assert "end" in phases


def test_verifier_preserves_state() -> None:
    out = verifier(_base_state(request="hello", intent="debug"))
    assert out["request"] == "hello"
    assert out["intent"] == "debug"
