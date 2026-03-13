from __future__ import annotations

from pathlib import Path

from lg_orch.procedure_cache import ProcedureCache, _canonical_procedure_name


def _make_cache(tmp_path: Path) -> ProcedureCache:
    return ProcedureCache(db_path=tmp_path / "procedures.sqlite")


def test_store_and_lookup_by_request_hash(tmp_path: Path) -> None:
    cache = _make_cache(tmp_path)
    steps = [{"id": "s1", "tools": [{"tool": "run_tests"}, {"tool": "check_output"}]}]
    cache.store_procedure(
        canonical_name="run_tests_check_output",
        request="run the tests",
        task_class="testing",
        steps=steps,
        verification=[],
        created_at="2026-01-01T00:00:00Z",
    )
    results = cache.lookup_procedure(request="run the tests")
    assert len(results) == 1
    assert results[0]["canonical_name"] == "run_tests_check_output"
    assert results[0]["steps"] == steps
    cache.close()


def test_lookup_no_match_returns_empty(tmp_path: Path) -> None:
    cache = _make_cache(tmp_path)
    cache.store_procedure(
        canonical_name="run_tests_check_output",
        request="run the tests",
        task_class="testing",
        steps=[{"id": "s1", "tools": [{"tool": "run_tests"}]}],
        verification=[],
        created_at="2026-01-01T00:00:00Z",
    )
    results = cache.lookup_procedure(request="completely different request")
    assert results == []
    cache.close()


def test_record_use_increments_count(tmp_path: Path) -> None:
    cache = _make_cache(tmp_path)
    pid = cache.store_procedure(
        canonical_name="run_tests_check_output",
        request="run the tests",
        task_class="testing",
        steps=[{"id": "s1", "tools": [{"tool": "run_tests"}]}],
        verification=[],
        created_at="2026-01-01T00:00:00Z",
    )
    cache.record_use(pid, used_at="2026-01-02T00:00:00Z")
    cache.record_use(pid, used_at="2026-01-03T00:00:00Z")
    results = cache.lookup_procedure(request="run the tests")
    assert results[0]["use_count"] == 2
    assert results[0]["last_used_at"] == "2026-01-03T00:00:00Z"
    cache.close()


def test_store_upsert_updates_steps(tmp_path: Path) -> None:
    cache = _make_cache(tmp_path)
    old_steps = [{"id": "s1", "tools": [{"tool": "run_tests"}]}]
    new_steps = [{"id": "s1", "tools": [{"tool": "run_tests"}]}, {"id": "s2", "tools": [{"tool": "check_output"}]}]
    cache.store_procedure(
        canonical_name="run_tests",
        request="run the tests",
        task_class="testing",
        steps=old_steps,
        verification=[],
        created_at="2026-01-01T00:00:00Z",
    )
    cache.store_procedure(
        canonical_name="run_tests",
        request="run the tests",
        task_class="testing",
        steps=new_steps,
        verification=[{"check": "passed"}],
        created_at="2026-01-02T00:00:00Z",
    )
    results = cache.lookup_procedure(request="run the tests")
    assert len(results) == 1
    assert results[0]["steps"] == new_steps
    cache.close()


def test_canonical_name_from_steps() -> None:
    steps = [
        {"id": "s1", "tools": [{"tool": "run_tests"}, {"tool": "check_output"}]},
        {"id": "s2", "tools": [{"tool": "apply_patch"}]},
    ]
    name = _canonical_procedure_name(steps)
    assert name == "run_tests_check_output_apply_patch"


def test_list_procedures(tmp_path: Path) -> None:
    cache = _make_cache(tmp_path)
    pid1 = cache.store_procedure(
        canonical_name="proc_a",
        request="request alpha",
        task_class="analysis",
        steps=[{"id": "s1", "tools": [{"tool": "tool_a"}]}],
        verification=[],
        created_at="2026-01-01T00:00:00Z",
    )
    pid2 = cache.store_procedure(
        canonical_name="proc_b",
        request="request beta",
        task_class="analysis",
        steps=[{"id": "s1", "tools": [{"tool": "tool_b"}]}],
        verification=[],
        created_at="2026-01-01T00:00:00Z",
    )
    cache.record_use(pid2, used_at="2026-01-02T00:00:00Z")
    cache.record_use(pid2, used_at="2026-01-03T00:00:00Z")
    rows = cache.list_procedures()
    assert rows[0]["procedure_id"] == pid2
    assert rows[0]["use_count"] == 2
    cache.close()


def test_empty_steps_not_stored() -> None:
    name = _canonical_procedure_name([])
    assert name == "unnamed_procedure"


def test_canonical_name_no_tool_field() -> None:
    steps = [{"id": "s1", "tools": [{"not_tool": "something"}]}]
    name = _canonical_procedure_name(steps)
    assert name == "unnamed_procedure"
