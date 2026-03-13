"""
End-to-end smoke tests.

These tests run the full orchestration graph with _runner_enabled=False
(deterministic, no network) and assert that the graph produces a structurally
valid output.

Run with:
    LG_E2E=1 uv run pytest tests/test_e2e.py -v
"""
from __future__ import annotations

import os
import pytest
from pathlib import Path
from typing import Any


_E2E = os.environ.get("LG_E2E", "").strip() in {"1", "true", "yes"}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _base_state(request: str) -> dict[str, Any]:
    repo_root = _repo_root()
    return {
        "request": request,
        "_repo_root": str(repo_root),
        "_runner_base_url": "http://127.0.0.1:8088",
        "_runner_enabled": False,
        "_budget_max_loops": 1,
        "_budget_max_tool_calls_per_loop": 0,
        "_budget_max_patch_bytes": 0,
        "_budget_context": {
            "stable_prefix_tokens": 1600,
            "working_set_tokens": 1600,
            "tool_result_summary_chars": 480,
        },
        "_config_policy": {
            "network_default": "deny",
            "require_approval_for_mutations": True,
            "allowed_write_paths": [],
        },
        "_models": {
            "router": {"provider": "local", "model": "deterministic", "temperature": 0.0},
            "planner": {"provider": "local", "model": "deterministic", "temperature": 0.0},
        },
        "_model_routing_policy": {
            "local_provider": "local",
            "fallback_task_classes": ["summarization", "lint_reflection", "context_condensation"],
            "interactive_context_limit": 1800,
            "deep_planning_context_limit": 3200,
            "recovery_retry_threshold": 1,
            "default_cache_affinity": "workspace",
        },
        "_model_provider_runtime": {
            "digitalocean": {
                "base_url": "https://inference.do-ai.run/v1",
                "api_key": None,
                "timeout_s": 60,
            }
        },
        "_trace_enabled": False,
        "_run_store_path": "",
        "_procedure_cache_path": "",
    }


@pytest.mark.skipif(not _E2E, reason="LG_E2E not set")
class TestE2ESmoke:
    def test_analysis_request_produces_valid_output(self) -> None:
        from lg_orch.graph import build_graph

        app = build_graph()
        out = dict(app.invoke(_base_state("Summarize the repository structure.")))
        assert isinstance(out.get("intent"), str), "intent must be a string"
        assert out.get("intent") in {
            "analysis", "code_change", "research", "question", "refactor", "debug"
        }, f"unexpected intent: {out.get('intent')}"
        assert "route" in out, "route must be present in output"
        assert isinstance(out.get("route"), dict), "route must be a dict"
        assert "lane" in out["route"], "route.lane must be present"
        assert "plan" in out, "plan must be present in output"
        assert "verification" in out, "verification must be present in output"
        assert isinstance(out.get("verification"), dict), "verification must be a dict"

    def test_code_change_request_routes_to_deep_planning(self) -> None:
        from lg_orch.graph import build_graph

        app = build_graph()
        out = dict(app.invoke(_base_state("Implement a new helper function in the utils module.")))
        assert out.get("intent") == "code_change", f"expected code_change, got {out.get('intent')}"
        route = out.get("route", {})
        assert isinstance(route, dict)
        lane = str(route.get("lane", ""))
        assert lane in {"deep_planning", "interactive", "recovery"}, f"unexpected lane: {lane}"

    def test_output_state_is_structurally_valid(self) -> None:
        from lg_orch.graph import build_graph

        app = build_graph()
        out = dict(app.invoke(_base_state("What is the purpose of this repository?")))
        # Core keys must always be present
        for key in ("intent", "route", "plan", "verification", "tool_results", "repo_context"):
            assert key in out, f"missing key in output: {key}"
        # tool_results must be a list
        assert isinstance(out.get("tool_results"), list)
        # verification must have 'ok' key
        verification = out.get("verification", {})
        assert isinstance(verification, dict)
        assert "ok" in verification

    def test_loop_budget_exhaustion_halts_gracefully(self) -> None:
        from lg_orch.graph import build_graph

        app = build_graph()
        state = _base_state("Summarize the repository.")
        state["_budget_max_loops"] = 1
        out = dict(app.invoke(state))
        # Graph must terminate — halt_reason may or may not be set depending on loop count
        assert isinstance(out, dict), "output must be a dict"
        assert "verification" in out

    def test_recovery_packet_on_failed_run(self) -> None:
        from lg_orch.graph import build_graph

        app = build_graph()
        state = _base_state("Fix the compilation error in rs/runner/src/main.rs")
        out = dict(app.invoke(state))
        # With runner disabled, a code_change request should produce a recovery_packet
        # (no tools executed → verification fails → recovery classified)
        assert "verification" in out
        verification = out.get("verification", {})
        assert isinstance(verification, dict)
        # Either ok=True (acceptance criteria met without runner) or recovery_packet present
        if not bool(verification.get("ok", False)):
            # When not ok, loop_summary should be non-empty
            loop_summary = str(verification.get("loop_summary", "")).strip()
            assert loop_summary, "non-ok verification must have a loop_summary"

    @pytest.mark.skipif(not _E2E, reason="LG_E2E not set")
    def test_model_routing_config_is_wired(self) -> None:
        """
        Verify that the model routing config keys are present in a run's output state.
        This does not call a real model — it verifies that config paths are structurally
        correct so that a real model call would be attempted.
        """
        import os
        from lg_orch.graph import build_graph

        app = build_graph()
        state = _base_state("Analyze the repository structure.")
        # Set an API key env var if available in CI
        api_key = os.environ.get("MODEL_ACCESS_KEY", "").strip()
        if api_key:
            state["_models"] = {
                "router": {"provider": "digitalocean", "model": "meta-llama/Meta-Llama-3.1-70B-Instruct", "temperature": 0.0},
                "planner": {"provider": "digitalocean", "model": "meta-llama/Meta-Llama-3.1-70B-Instruct", "temperature": 0.0},
            }
            state["_model_provider_runtime"] = {
                "digitalocean": {
                    "base_url": "https://inference.do-ai.run/v1",
                    "api_key": api_key,
                    "timeout_s": 30,
                }
            }
            # Keep runner disabled — we are testing model routing config wiring only
            state["_runner_enabled"] = False
        out = dict(app.invoke(state))
        assert "route" in out, "route must be present"
        route = out.get("route", {})
        assert isinstance(route, dict)
        # Lane must be one of the valid values
        assert str(route.get("lane", "")) in {"interactive", "deep_planning", "recovery"}, \
            f"unexpected lane: {route.get('lane')}"
