from __future__ import annotations

from pathlib import Path

from lg_orch.run_store import RunStore


def _make_record(run_id: str = "run1", status: str = "running") -> dict:
    return {
        "run_id": run_id,
        "request": "do something",
        "status": status,
        "created_at": "2026-01-01T00:00:00Z",
        "started_at": "2026-01-01T00:00:00Z",
        "finished_at": None,
        "exit_code": None,
        "trace_out_dir": "artifacts/runs",
        "trace_path": f"artifacts/runs/run-{run_id}.json",
        "request_id": "req-abc",
        "auth_subject": "",
        "client_ip": "127.0.0.1",
        "thread_id": "thread-1",
        "checkpoint_id": "cp-1",
        "pending_approval": False,
        "pending_approval_summary": "",
    }


def test_create_table_and_upsert(tmp_path: Path) -> None:
    store = RunStore(db_path=tmp_path / "runs.sqlite")
    try:
        store.upsert(_make_record())
        rows = store.list_runs()
        assert len(rows) == 1
        assert rows[0]["run_id"] == "run1"
    finally:
        store.close()


def test_get_run(tmp_path: Path) -> None:
    store = RunStore(db_path=tmp_path / "runs.sqlite")
    try:
        store.upsert(_make_record("r2"))
        row = store.get_run("r2")
        assert row is not None
        assert row["run_id"] == "r2"
        assert row["request"] == "do something"
    finally:
        store.close()


def test_get_run_missing(tmp_path: Path) -> None:
    store = RunStore(db_path=tmp_path / "runs.sqlite")
    try:
        assert store.get_run("nonexistent") is None
    finally:
        store.close()


def test_list_runs_empty(tmp_path: Path) -> None:
    store = RunStore(db_path=tmp_path / "runs.sqlite")
    try:
        assert store.list_runs() == []
    finally:
        store.close()


def test_list_runs_ordered_by_created_at_desc(tmp_path: Path) -> None:
    store = RunStore(db_path=tmp_path / "runs.sqlite")
    try:
        r1 = _make_record("r1")
        r1["created_at"] = "2026-01-01T00:00:00Z"
        r2 = _make_record("r2")
        r2["created_at"] = "2026-01-02T00:00:00Z"
        store.upsert(r1)
        store.upsert(r2)
        rows = store.list_runs()
        assert rows[0]["run_id"] == "r2"
        assert rows[1]["run_id"] == "r1"
    finally:
        store.close()


def test_upsert_idempotent_update(tmp_path: Path) -> None:
    store = RunStore(db_path=tmp_path / "runs.sqlite")
    try:
        store.upsert(_make_record("r3", status="running"))
        updated = _make_record("r3", status="succeeded")
        updated["exit_code"] = 0
        updated["finished_at"] = "2026-01-01T00:01:00Z"
        store.upsert(updated)
        row = store.get_run("r3")
        assert row is not None
        assert row["status"] == "succeeded"
        assert row["exit_code"] == 0
        assert row["finished_at"] == "2026-01-01T00:01:00Z"
    finally:
        store.close()


def test_upsert_persists_approval_summary_fields(tmp_path: Path) -> None:
    store = RunStore(db_path=tmp_path / "runs.sqlite")
    try:
        record = _make_record("approval-run", status="suspended")
        record["pending_approval"] = True
        record["pending_approval_summary"] = "apply_patch requires approval"
        store.upsert(record)
        row = store.get_run("approval-run")
        assert row is not None
        assert row["thread_id"] == "thread-1"
        assert row["checkpoint_id"] == "cp-1"
        assert row["pending_approval"] == 1
        assert row["pending_approval_summary"] == "apply_patch requires approval"
    finally:
        store.close()


def test_upsert_unknown_keys_ignored(tmp_path: Path) -> None:
    store = RunStore(db_path=tmp_path / "runs.sqlite")
    try:
        record = _make_record("r4")
        record["log_lines"] = 42  # type: ignore[assignment]  # not a DB column
        record["cancel_requested"] = True  # type: ignore[assignment]
        store.upsert(record)
        row = store.get_run("r4")
        assert row is not None
        assert row["run_id"] == "r4"
    finally:
        store.close()


def test_db_created_on_disk(tmp_path: Path) -> None:
    db_path = tmp_path / "sub" / "runs.sqlite"
    store = RunStore(db_path=db_path)
    store.close()
    assert db_path.exists()


# ---------------------------------------------------------------------------
# recovery_facts / episodic memory
# ---------------------------------------------------------------------------

def _make_fact(
    fingerprint: str = "fp1",
    failure_class: str = "lint",
    summary: str = "test failed",
    loop: int = 1,
    salience: int = 5,
) -> dict:
    return {
        "failure_fingerprint": fingerprint,
        "failure_class": failure_class,
        "summary": summary,
        "loop": loop,
        "salience": salience,
        "last_check": "ruff",
        "context_scope": "py/",
        "plan_action": "retry",
    }


def test_upsert_recovery_facts_stores_rows(tmp_path: Path) -> None:
    store = RunStore(db_path=tmp_path / "runs.sqlite")
    try:
        facts = [_make_fact("fp1"), _make_fact("fp2", failure_class="typecheck")]
        store.upsert_recovery_facts("run-A", facts)
        rows = store.get_recent_recovery_facts()
        fingerprints = {r["fingerprint"] for r in rows}
        assert "fp1" in fingerprints
        assert "fp2" in fingerprints
    finally:
        store.close()


def test_get_recent_recovery_facts_by_fingerprint(tmp_path: Path) -> None:
    store = RunStore(db_path=tmp_path / "runs.sqlite")
    try:
        store.upsert_recovery_facts("run-B", [_make_fact("target_fp"), _make_fact("other_fp")])
        rows = store.get_recent_recovery_facts(fingerprint="target_fp")
        assert len(rows) == 1
        assert rows[0]["fingerprint"] == "target_fp"
    finally:
        store.close()


def test_get_recent_recovery_facts_by_class(tmp_path: Path) -> None:
    store = RunStore(db_path=tmp_path / "runs.sqlite")
    try:
        store.upsert_recovery_facts("run-C", [
            _make_fact("fp3", failure_class="mypy"),
            _make_fact("fp4", failure_class="lint"),
        ])
        # fingerprint lookup yields nothing for "fp_nope", falls back to failure_class
        rows = store.get_recent_recovery_facts(fingerprint="fp_nope", failure_class="mypy")
        assert len(rows) == 1
        assert rows[0]["failure_class"] == "mypy"
    finally:
        store.close()


def test_upsert_recovery_facts_skips_empty_fingerprint(tmp_path: Path) -> None:
    store = RunStore(db_path=tmp_path / "runs.sqlite")
    try:
        bad_fact: dict = {"failure_fingerprint": "", "summary": "should be ignored"}
        store.upsert_recovery_facts("run-D", [bad_fact])
        rows = store.get_recent_recovery_facts()
        assert rows == []
    finally:
        store.close()


def test_get_episodic_context_returns_empty_when_no_match(tmp_path: Path) -> None:
    store = RunStore(db_path=tmp_path / "runs.sqlite")
    try:
        result = store.get_episodic_context(
            failure_fingerprint="no_such_fp",
            failure_class="no_such_class",
        )
        assert result == []
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Namespace isolation
# ---------------------------------------------------------------------------


def test_namespace_isolation(tmp_path: Path) -> None:
    db = tmp_path / "runs.sqlite"
    store_a = RunStore(db_path=db, namespace="a")
    store_b = RunStore(db_path=db, namespace="b")
    try:
        store_a.upsert(_make_record("ns-run-1"))
        assert len(store_a.list_runs()) == 1
        assert store_a.list_runs()[0]["run_id"] == "ns-run-1"
        assert store_b.list_runs() == []
    finally:
        store_a.close()
        store_b.close()


def test_recovery_facts_namespace_isolation(tmp_path: Path) -> None:
    db = tmp_path / "runs.sqlite"
    store_a = RunStore(db_path=db, namespace="ns-a")
    store_b = RunStore(db_path=db, namespace="ns-b")
    try:
        store_a.upsert_recovery_facts("run-X", [_make_fact("fp-ns-a")])
        rows_a = store_a.get_recent_recovery_facts()
        rows_b = store_b.get_recent_recovery_facts()
        assert len(rows_a) == 1
        assert rows_a[0]["fingerprint"] == "fp-ns-a"
        assert rows_b == []
    finally:
        store_a.close()
        store_b.close()


def test_migration_adds_column_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "runs.sqlite"
    store1 = RunStore(db_path=db)
    store1.upsert(_make_record("idem-1"))
    store1.close()
    # Opening the same db again should not raise
    store2 = RunStore(db_path=db)
    rows = store2.list_runs()
    assert any(r["run_id"] == "idem-1" for r in rows)
    store2.close()
