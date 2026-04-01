"""Targeted tests for pure helper functions across modules to improve coverage."""

from __future__ import annotations

from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# context_builder helpers
# ---------------------------------------------------------------------------


def test_semantic_query_from_request_extracts_tokens() -> None:
    from lg_orch.nodes.context_builder import _semantic_query_from_request

    result = _semantic_query_from_request("implement the login feature")
    assert "implement" in result
    assert "login" in result


def test_semantic_query_from_request_limits_to_8_tokens() -> None:
    from lg_orch.nodes.context_builder import _semantic_query_from_request

    result = _semantic_query_from_request("a b c d e f g h i j k")
    tokens = result.split()
    assert len(tokens) <= 8


def test_semantic_query_from_request_empty_returns_default() -> None:
    from lg_orch.nodes.context_builder import _semantic_query_from_request

    assert _semantic_query_from_request("") == "repository structure"


def test_validate_base_url_helper_returns_bool() -> None:
    from lg_orch.nodes.context_builder import _validate_base_url

    assert _validate_base_url("http://localhost:8088") is True
    assert _validate_base_url("ftp://bad") is False


def test_runner_client_from_state_returns_none_when_disabled() -> None:
    from lg_orch.nodes.context_builder import _runner_client_from_state

    result = _runner_client_from_state({"_runner_enabled": False})
    assert result is None


def test_runner_client_from_state_returns_none_for_missing_url() -> None:
    from lg_orch.nodes.context_builder import _runner_client_from_state

    result = _runner_client_from_state({"_runner_enabled": True})
    assert result is None


def test_runner_client_from_state_returns_none_for_bad_url() -> None:
    from lg_orch.nodes.context_builder import _runner_client_from_state

    result = _runner_client_from_state({"_runner_enabled": True, "_runner_base_url": "not-a-url"})
    assert result is None


def test_runner_client_from_state_returns_client_for_valid_url() -> None:
    from lg_orch.nodes.context_builder import _runner_client_from_state

    client = _runner_client_from_state(
        {"_runner_enabled": True, "_runner_base_url": "http://localhost:8088"}
    )
    assert client is not None
    client.close()


# ---------------------------------------------------------------------------
# coder._coerce_handoff
# ---------------------------------------------------------------------------


def test_coerce_handoff_returns_none_for_non_dict() -> None:
    from lg_orch.nodes.coder import _coerce_handoff

    assert _coerce_handoff("not a dict") is None
    assert _coerce_handoff(None) is None


def test_coerce_handoff_returns_none_for_missing_fields() -> None:
    from lg_orch.nodes.coder import _coerce_handoff

    assert _coerce_handoff({"producer": "planner"}) is None
    assert _coerce_handoff({"producer": "p", "consumer": "c"}) is None


def test_coerce_handoff_returns_dict_for_valid_input() -> None:
    from lg_orch.nodes.coder import _coerce_handoff

    result = _coerce_handoff(
        {
            "producer": "planner",
            "consumer": "coder",
            "objective": "implement feature",
        }
    )
    assert result is not None
    assert result["producer"] == "planner"
    assert result["consumer"] == "coder"
    assert result["objective"] == "implement feature"


# ---------------------------------------------------------------------------
# reporter._get_inference_config
# ---------------------------------------------------------------------------


def test_reporter_summarize_tool_results_with_stderr() -> None:
    from lg_orch.nodes.reporter import _summarize_tool_results

    results = [
        {"tool": "exec", "ok": True, "stdout": "output", "stderr": "warning"},
        "not_a_dict",  # should be skipped
        {"tool": "search", "ok": False, "stdout": "", "stderr": "error msg"},
    ]
    summary = _summarize_tool_results(results)
    assert "[exec] ok=True" in summary
    assert "output" in summary
    assert "warning" in summary
    assert "[search] ok=False" in summary
    assert "error msg" in summary


def test_reporter_structured_summary() -> None:
    from lg_orch.nodes.reporter import _structured_summary

    state = {
        "intent": "code_change",
        "repo_context": {"repo_root": "/tmp/repo", "top_level": ["py/", "docs/"]},
        "tool_results": [{"tool": "exec"}],
        "verification": {
            "ok": True,
            "acceptance_ok": False,
            "acceptance_checks": [
                {"criterion": "tests pass", "ok": True},
                {"criterion": "lint clean", "ok": False},
            ],
        },
        "halt_reason": "plan_done",
    }
    summary = _structured_summary(state)
    assert "code_change" in summary
    assert "verification_ok: True" in summary
    assert "acceptance_ok: False" in summary
    assert "lint clean" in summary
    assert "plan_done" in summary


def test_reporter_get_inference_config_returns_none_for_local() -> None:
    from lg_orch.nodes.reporter import _get_inference_config

    result = _get_inference_config(
        {
            "_models": {"planner": {"provider": "local", "model": "det"}},
        }
    )
    assert result is None


def test_reporter_get_inference_config_returns_none_for_missing_model() -> None:
    from lg_orch.nodes.reporter import _get_inference_config

    result = _get_inference_config(
        {
            "_models": {"planner": {"provider": "digitalocean", "model": ""}},
        }
    )
    assert result is None


def test_reporter_get_inference_config_returns_tuple_with_valid_do_config() -> None:
    from lg_orch.nodes.reporter import _get_inference_config

    result = _get_inference_config(
        {
            "_models": {"planner": {"provider": "digitalocean", "model": "gpt-4.1"}},
            "_model_provider_runtime": {
                "digitalocean": {"api_key": "sk-test", "base_url": "https://inference.do-ai.run/v1"}
            },
        }
    )
    assert result is not None
    model, api_key, base_url, timeout_s = result
    assert model == "gpt-4.1"
    assert api_key == "sk-test"
    assert "inference.do-ai.run" in base_url
    assert timeout_s == 60


def test_reporter_get_inference_config_returns_tuple_with_valid_openai_config() -> None:
    from lg_orch.nodes.reporter import _get_inference_config

    result = _get_inference_config(
        {
            "_models": {"planner": {"provider": "openai_compatible", "model": "gpt-4o"}},
            "_model_provider_runtime": {
                "openai_compatible": {"api_key": "sk-test", "base_url": "https://api.openai.com/v1"}
            },
        }
    )
    assert result is not None
    model, api_key, _base_url, _timeout_s = result
    assert model == "gpt-4o"
    assert api_key == "sk-test"


def test_reporter_get_inference_config_returns_none_for_bad_base_url() -> None:
    from lg_orch.nodes.reporter import _get_inference_config

    result = _get_inference_config(
        {
            "_models": {"planner": {"provider": "digitalocean", "model": "gpt-4.1"}},
            "_model_provider_runtime": {
                "digitalocean": {"api_key": "sk-test", "base_url": "ftp://bad"}
            },
        }
    )
    assert result is None


def test_reporter_get_inference_config_uses_env_do_key() -> None:
    import os

    from lg_orch.nodes.reporter import _get_inference_config

    os.environ["MODEL_ACCESS_KEY"] = "sk-from-env"
    try:
        result = _get_inference_config(
            {
                "_models": {"planner": {"provider": "digitalocean", "model": "gpt-4.1"}},
                "_model_provider_runtime": {"digitalocean": {}},
            }
        )
        assert result is not None
        _, api_key, _, _ = result
        assert api_key == "sk-from-env"
    finally:
        os.environ.pop("MODEL_ACCESS_KEY", None)


def test_reporter_get_inference_config_uses_env_openai_key() -> None:
    import os

    from lg_orch.nodes.reporter import _get_inference_config

    os.environ["OPENAI_API_KEY"] = "sk-openai-env"
    try:
        result = _get_inference_config(
            {
                "_models": {"planner": {"provider": "openai_compatible", "model": "gpt-4o"}},
                "_model_provider_runtime": {"openai_compatible": {}},
            }
        )
        assert result is not None
        _, api_key, _, _ = result
        assert api_key == "sk-openai-env"
    finally:
        os.environ.pop("OPENAI_API_KEY", None)


def test_reporter_get_inference_config_returns_none_for_missing_api_key() -> None:
    import os

    for key in ("MODEL_ACCESS_KEY", "DIGITAL_OCEAN_MODEL_ACCESS_KEY"):
        os.environ.pop(key, None)

    from lg_orch.nodes.reporter import _get_inference_config

    result = _get_inference_config(
        {
            "_models": {"planner": {"provider": "digitalocean", "model": "gpt-4.1"}},
            "_model_provider_runtime": {"digitalocean": {"api_key": ""}},
        }
    )
    assert result is None


# ---------------------------------------------------------------------------
# trace helpers
# ---------------------------------------------------------------------------


def test_trace_state_get_with_dict() -> None:
    from lg_orch.trace import _state_get

    assert _state_get({"key": "val"}, "key") == "val"
    assert _state_get({"key": "val"}, "missing", "default") == "default"


def test_trace_state_get_with_non_dict() -> None:
    from lg_orch.trace import _state_get

    assert _state_get("not a dict", "key", "default") == "default"


def test_trace_state_as_dict_with_dict() -> None:
    from lg_orch.trace import _state_as_dict

    result = _state_as_dict({"a": 1, "b": 2})
    assert result == {"a": 1, "b": 2}


def test_trace_state_as_dict_with_pydantic_model() -> None:
    from lg_orch.state import OrchState
    from lg_orch.trace import _state_as_dict

    model = OrchState(request="test")
    result = _state_as_dict(model)
    assert result["request"] == "test"


def test_trace_ensure_run_id_generates_when_missing() -> None:
    from lg_orch.trace import ensure_run_id

    result = ensure_run_id({})
    assert "_run_id" in result
    assert len(result["_run_id"]) == 32


def test_trace_ensure_run_id_preserves_existing() -> None:
    from lg_orch.trace import ensure_run_id

    result = ensure_run_id({"_run_id": "existing"})
    assert result["_run_id"] == "existing"


# ---------------------------------------------------------------------------
# model_routing: LatencyWindow and SlaRoutingPolicy
# ---------------------------------------------------------------------------


def test_latency_window_needs_min_samples_for_p95() -> None:
    from lg_orch.model_routing import LatencyWindow

    window = LatencyWindow("test-model")
    for _ in range(4):
        window.record(1.0)
    assert window.p95() is None  # Less than 5 samples
    window.record(2.0)
    assert window.p95() is not None  # Now has 5 samples


def test_latency_window_model_id_property() -> None:
    from lg_orch.model_routing import LatencyWindow

    window = LatencyWindow("test-model")
    assert window.model_id == "test-model"
    assert window.sample_count() == 0


def test_latency_window_p95_computation() -> None:
    from lg_orch.model_routing import LatencyWindow

    window = LatencyWindow("m", window_size=10)
    for v in [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]:
        window.record(v)
    assert window.sample_count() == 10
    p95 = window.p95()
    assert p95 is not None
    assert p95 >= 9.0  # 95th percentile of 1..10


def test_sla_routing_policy_select_model_no_window() -> None:
    from lg_orch.model_routing import SlaRoutingPolicy

    policy = SlaRoutingPolicy(thresholds={}, fallbacks={})
    assert policy.select_model("model-a") == "model-a"


def test_sla_routing_policy_select_model_no_threshold() -> None:
    from lg_orch.model_routing import SlaRoutingPolicy

    policy = SlaRoutingPolicy(thresholds={}, fallbacks={})
    for _ in range(10):
        policy.record_latency("model-a", 2.0)
    assert policy.select_model("model-a") == "model-a"


def test_sla_routing_policy_select_model_falls_back() -> None:
    from lg_orch.model_routing import SlaRoutingPolicy

    policy = SlaRoutingPolicy(
        thresholds={"model-a": 1.0},
        fallbacks={"model-a": "model-b"},
    )
    for _ in range(10):
        policy.record_latency("model-a", 2.0)
    assert policy.select_model("model-a") == "model-b"


def test_sla_routing_policy_select_model_stays_when_under_threshold() -> None:
    from lg_orch.model_routing import SlaRoutingPolicy

    policy = SlaRoutingPolicy(
        thresholds={"model-a": 5.0},
        fallbacks={"model-a": "model-b"},
    )
    for _ in range(10):
        policy.record_latency("model-a", 1.0)
    assert policy.select_model("model-a") == "model-a"


def test_diversity_routing_policy_rejects_empty_models() -> None:
    from lg_orch.model_routing import DiversityRoutingPolicy

    with pytest.raises(ValueError, match="at least one model"):
        DiversityRoutingPolicy(models=[])


def test_build_sla_policy_returns_none_for_empty_config() -> None:
    from lg_orch.model_routing import SlaConfig, build_sla_policy

    assert build_sla_policy(SlaConfig()) is None


def test_build_sla_policy_returns_policy_for_valid_config() -> None:
    from lg_orch.model_routing import SlaConfig, SlaEntry, build_sla_policy

    cfg = SlaConfig(entries=[SlaEntry(model_id="m1", threshold_p95_s=2.0, fallback_model_id="m2")])
    policy = build_sla_policy(cfg)
    assert policy is not None
    assert policy.select_model("m1") == "m1"  # No latency data yet


def test_sla_routing_policy_degraded_models() -> None:
    from lg_orch.model_routing import SlaRoutingPolicy

    policy = SlaRoutingPolicy(
        thresholds={"model-a": 1.0},
        fallbacks={"model-a": "model-b"},
    )
    # No samples yet
    assert policy.degraded_models() == []

    # Add samples that exceed threshold
    for _ in range(10):
        policy.record_latency("model-a", 2.0)

    assert "model-a" in policy.degraded_models()


# ---------------------------------------------------------------------------
# model_routing: tool_routing_metadata
# ---------------------------------------------------------------------------


def test_tool_routing_metadata_with_route_data() -> None:
    from lg_orch.model_routing import tool_routing_metadata

    state = {
        "route": {
            "lane": "deep_planning",
            "provider": "remote",
            "model": "gpt-4.1",
            "task_class": "deep_planning",
            "cache_affinity": "workspace:planner:0",
            "prefix_segment": "stable_prefix",
        },
        "telemetry": {
            "model_routing": [
                {
                    "node": "planner",
                    "provider": "remote_openai",
                    "model": "gpt-4.1",
                    "lane": "deep_planning",
                    "task_class": "deep_planning",
                    "cache_affinity": "workspace",
                    "prefix_segment": "stable_prefix",
                }
            ]
        },
    }
    meta = tool_routing_metadata(state, stage="pre_exec")
    assert meta["stage"] == "pre_exec"
    assert meta["lane"] == "deep_planning"
    assert meta["provider"] == "remote_openai"


def test_tool_routing_metadata_with_empty_state() -> None:
    from lg_orch.model_routing import tool_routing_metadata

    meta = tool_routing_metadata({}, stage="test")
    assert meta["stage"] == "test"
    assert meta["lane"] == "interactive"


# ---------------------------------------------------------------------------
# model_routing: record_inference_telemetry edge cases
# ---------------------------------------------------------------------------


def test_record_inference_telemetry_with_none_response() -> None:
    from lg_orch.model_routing import record_inference_telemetry

    state = {"route": {"lane": "interactive"}, "telemetry": {}}
    out = record_inference_telemetry(
        state, node_name="planner", provider="local", model="det", response=None
    )
    entry = out["telemetry"]["inference"][-1]
    assert entry["node"] == "planner"
    assert entry["latency_ms"] == 0


# ---------------------------------------------------------------------------
# _planner_prompt helpers
# ---------------------------------------------------------------------------


def test_first_step_handoff_returns_none_for_non_list_steps() -> None:
    from lg_orch.nodes._planner_prompt import _first_step_handoff

    assert _first_step_handoff({"steps": "not_a_list"}) is None


def test_first_step_handoff_skips_non_dict_steps() -> None:
    from lg_orch.nodes._planner_prompt import _first_step_handoff

    result = _first_step_handoff({"steps": ["not_a_dict", {"handoff": {"key": "val"}}]})
    assert result == {"key": "val"}


def test_first_step_handoff_returns_none_for_no_handoff() -> None:
    from lg_orch.nodes._planner_prompt import _first_step_handoff

    result = _first_step_handoff({"steps": [{"tool": "exec"}]})
    assert result is None


def test_extract_pdf_path_returns_none_for_no_match() -> None:
    from lg_orch.nodes._planner_prompt import _extract_pdf_path

    assert _extract_pdf_path("no pdf here") is None


def test_extract_pdf_path_returns_path() -> None:
    from lg_orch.nodes._planner_prompt import _extract_pdf_path

    result = _extract_pdf_path("analyze the file docs/report.pdf")
    assert result is not None
    assert "report.pdf" in result


def test_classify_intent_debug() -> None:
    from lg_orch.nodes._planner_prompt import _classify_intent

    assert _classify_intent("debug the failing test") == "debug"


def test_classify_intent_research() -> None:
    from lg_orch.nodes._planner_prompt import _classify_intent

    assert _classify_intent("research the latest API changes") == "research"


def test_classify_intent_question() -> None:
    from lg_orch.nodes._planner_prompt import _classify_intent

    assert _classify_intent("why does this happen?") == "question"


def test_classify_intent_code_change() -> None:
    from lg_orch.nodes._planner_prompt import _classify_intent

    assert _classify_intent("implement login feature") == "code_change"


def test_recovery_action_from_packet() -> None:
    from lg_orch.nodes._planner_prompt import _recovery_action_from_packet

    packet = {
        "failure_class": "test_failure",
        "context_scope": "working_set",
        "failing_checks": [{"name": "test_a", "summary": "failed"}],
    }
    result = _recovery_action_from_packet(packet)
    assert result is not None
    assert isinstance(result, dict)


def test_build_planner_prompts_with_repair_mode(tmp_path: Path) -> None:
    from lg_orch.nodes._planner_prompt import _build_planner_prompts

    state = {
        "request": "fix the test",
        "test_repair_mode": True,
        "_repo_root": str(tmp_path),
    }
    sys_prompt, _user_prompt = _build_planner_prompts(
        state, repo_root=tmp_path, repo_context={}, route={}, verification={}
    )
    assert "REPAIR MODE" in sys_prompt


def test_build_planner_prompts_without_repair_mode(tmp_path: Path) -> None:
    from lg_orch.nodes._planner_prompt import _build_planner_prompts

    state = {
        "request": "analyze code",
        "_repo_root": str(tmp_path),
    }
    sys_prompt, user_prompt = _build_planner_prompts(
        state, repo_root=tmp_path, repo_context={}, route={}, verification={}
    )
    assert "REPAIR MODE" not in sys_prompt
    assert "analyze code" in user_prompt


def test_build_planner_prompts_loads_custom_prompt(tmp_path: Path) -> None:
    from lg_orch.nodes._planner_prompt import _build_planner_prompts

    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "planner.md").write_text("Custom planner prompt.", encoding="utf-8")

    state = {"request": "test", "_repo_root": str(tmp_path)}
    sys_prompt, _ = _build_planner_prompts(
        state, repo_root=tmp_path, repo_context={}, route={}, verification={}
    )
    assert "Custom planner prompt." in sys_prompt


# ---------------------------------------------------------------------------
# api.approvals helpers
# ---------------------------------------------------------------------------


def test_tool_name_for_approval_apply_patch() -> None:
    from lg_orch.api.approvals import tool_name_for_approval

    assert tool_name_for_approval(operation_class="apply_patch", challenge_id="c1") == "apply_patch"


def test_tool_name_for_approval_exec() -> None:
    from lg_orch.api.approvals import tool_name_for_approval

    assert tool_name_for_approval(operation_class="exec_command", challenge_id="c1") == "exec"


def test_tool_name_for_approval_default() -> None:
    from lg_orch.api.approvals import tool_name_for_approval

    assert tool_name_for_approval(operation_class="unknown", challenge_id="c1") == "apply_patch"


def test_approval_summary_text_basic() -> None:
    from lg_orch.api.approvals import approval_summary_text

    result = approval_summary_text({"operation_class": "patch", "challenge_id": "ch-1"})
    assert "patch requires approval" in result
    assert "ch-1" in result


def test_approval_summary_text_with_custom_reason() -> None:
    from lg_orch.api.approvals import approval_summary_text

    result = approval_summary_text({"reason": "dangerous_operation"})
    assert "dangerous_operation" in result


def test_non_empty_str_returns_none_for_non_string() -> None:
    from lg_orch.api.approvals import _non_empty_str

    assert _non_empty_str(123) is None
    assert _non_empty_str(None) is None


def test_non_empty_str_returns_none_for_blank() -> None:
    from lg_orch.api.approvals import _non_empty_str

    assert _non_empty_str("") is None
    assert _non_empty_str("   ") is None


def test_non_empty_str_returns_stripped_value() -> None:
    from lg_orch.api.approvals import _non_empty_str

    assert _non_empty_str("  hello  ") == "hello"


def test_approval_summary_text_no_details() -> None:
    from lg_orch.api.approvals import approval_summary_text

    result = approval_summary_text({})
    assert "mutation requires approval" in result


def test_decide_model_route_deep_planning_for_high_fact_count() -> None:
    from lg_orch.model_routing import decide_model_route

    decision = decide_model_route(
        task_class="analysis",
        primary_provider="remote",
        primary_model="gpt-4.1",
        local_provider="local",
        fallback_task_classes=(),
        lane="interactive",
        context_tokens=500,
        latency_sensitive=True,
        fact_count=5,
    )
    assert decision.reason == "compression_pressure_capability_path"


# ---------------------------------------------------------------------------
# commands/trace helpers
# ---------------------------------------------------------------------------


def test_commands_trace_payload_from_path_valid(tmp_path: Path) -> None:
    from lg_orch.commands.trace import _trace_payload_from_path

    f = tmp_path / "trace.json"
    f.write_text('{"run_id": "x"}', encoding="utf-8")
    result = _trace_payload_from_path(f, warn_context="test")
    assert result == {"run_id": "x"}


def test_commands_trace_payload_from_path_missing(tmp_path: Path) -> None:
    from lg_orch.commands.trace import _trace_payload_from_path

    result = _trace_payload_from_path(tmp_path / "missing.json", warn_context="test")
    assert result is None


def test_commands_trace_payload_from_path_bad_json(tmp_path: Path) -> None:
    from lg_orch.commands.trace import _trace_payload_from_path

    f = tmp_path / "bad.json"
    f.write_text("{broken", encoding="utf-8")
    result = _trace_payload_from_path(f, warn_context="test")
    assert result is None


def test_commands_trace_payload_from_path_non_dict(tmp_path: Path) -> None:
    from lg_orch.commands.trace import _trace_payload_from_path

    f = tmp_path / "arr.json"
    f.write_text("[1,2]", encoding="utf-8")
    result = _trace_payload_from_path(f, warn_context="test")
    assert result is None


def test_commands_trace_run_id() -> None:
    from lg_orch.commands.trace import _trace_run_id

    assert _trace_run_id(Path("/tmp/run-abc.json"), {"run_id": "from-payload"}) == "from-payload"
    assert _trace_run_id(Path("/tmp/run-fallback.json"), {}) == "fallback"


def test_record_inference_telemetry_with_string_response() -> None:
    from lg_orch.model_routing import record_inference_telemetry

    state = {"route": {}, "telemetry": {}}
    out = record_inference_telemetry(
        state, node_name="coder", provider="test", model="m", response="raw text"
    )
    entry = out["telemetry"]["inference"][-1]
    assert entry["node"] == "coder"


# ---------------------------------------------------------------------------
# _planner_memory helpers
# ---------------------------------------------------------------------------


def test_apply_semantic_memory_constraints_no_memories() -> None:
    from lg_orch.nodes._planner_memory import _apply_semantic_memory_constraints

    plan = {"steps": [{"tool": "exec"}]}
    result = _apply_semantic_memory_constraints(plan, repo_context={}, request="test")
    assert "steps" in result


def test_apply_semantic_memory_constraints_with_memories() -> None:
    from lg_orch.nodes._planner_memory import _apply_semantic_memory_constraints

    plan = {"steps": [{"tool": "exec"}]}
    repo_context = {
        "semantic_memories": [
            {"kind": "loop_summary", "summary": "previously fixed auth module"},
        ]
    }
    result = _apply_semantic_memory_constraints(plan, repo_context=repo_context, request="fix auth")
    assert "steps" in result


def test_apply_procedural_memory_constraints_no_procedures() -> None:
    from lg_orch.nodes._planner_memory import _apply_procedural_memory_constraints

    plan = {"steps": [{"tool": "exec"}]}
    result_plan, proc_id = _apply_procedural_memory_constraints(
        plan, repo_context={}, request="test"
    )
    assert "steps" in result_plan
    assert proc_id is None


def test_record_selected_procedure_use_no_id() -> None:
    from lg_orch.nodes._planner_memory import _record_selected_procedure_use

    # Should not crash when procedure_id is None
    _record_selected_procedure_use({}, procedure_id=None)
