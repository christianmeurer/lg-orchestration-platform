from __future__ import annotations

from typing import Any

from lg_orch.nodes.coder import coder


def _base_state(**overrides: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "request": "implement a helper",
        "plan": {
            "steps": [
                {
                    "id": "step-1",
                    "description": "Prepare the helper patch.",
                    "tools": [{"tool": "apply_patch", "input": {"changes": []}}],
                    "expected_outcome": "Helper patch prepared.",
                    "files_touched": ["py/src/lg_orch/state.py"],
                    "handoff": {
                        "producer": "planner",
                        "consumer": "coder",
                        "objective": "Prepare a minimal patch.",
                        "file_scope": [],
                        "evidence": [],
                        "constraints": ["Prefer the smallest correct diff."],
                        "acceptance_checks": ["The patch addresses the request."],
                        "retry_budget": 1,
                        "provenance": ["plan:step-1"],
                    },
                }
            ]
        },
        "active_handoff": {
            "producer": "planner",
            "consumer": "coder",
            "objective": "Prepare a minimal patch.",
            "file_scope": [],
            "evidence": [],
            "constraints": ["Prefer the smallest correct diff."],
            "acceptance_checks": ["The patch addresses the request."],
            "retry_budget": 1,
            "provenance": ["plan:step-1"],
        },
        "retry_target": "coder",
    }
    state.update(overrides)
    return state


def test_coder_transforms_handoff_to_executor() -> None:
    out = coder(_base_state())
    assert out["active_handoff"]["producer"] == "coder"
    assert out["active_handoff"]["consumer"] == "executor"
    assert "py/src/lg_orch/state.py" in out["active_handoff"]["file_scope"]
    assert out["retry_target"] is None


def test_coder_passes_through_when_no_coder_handoff_present() -> None:
    out = coder(_base_state(active_handoff=None, plan={"steps": [{"id": "step-1", "description": "x", "tools": [], "expected_outcome": "ok", "files_touched": []}]}))
    assert out.get("active_handoff") is None
    events = out.get("_trace_events", [])
    assert events[-1]["data"]["handoff"] == "pass_through"
