from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from lg_orch.checkpointing import (
    CheckpointBackendError,
    PostgresCheckpointSaver,
    RedisCheckpointSaver,
    SqliteCheckpointSaver,
    create_checkpoint_saver,
    resolve_checkpoint_db_path,
    stable_checkpoint_thread_id,
)
from lg_orch.graph import build_graph
from lg_orch.run_store import RunStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _base_state() -> dict[str, object]:
    return {
        "request": "summarize repo",
        "_repo_root": ".",
        "_runner_base_url": "http://127.0.0.1:8088",
        "_runner_enabled": False,
        "_budget_max_loops": 1,
        "_config_policy": {
            "network_default": "deny",
            "require_approval_for_mutations": True,
        },
    }


def _make_fake_checkpoint(checkpoint_id: str) -> dict[str, Any]:
    """Return a minimal dict that satisfies the LangGraph Checkpoint TypedDict."""
    return {
        "v": 1,
        "id": checkpoint_id,
        "ts": datetime.now(UTC).isoformat(),
        "channel_values": {},
        "channel_versions": {},
        "versions_seen": {},
        "pending_sends": [],
    }


def _make_fake_config(thread_id: str, checkpoint_id: str | None = None) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "configurable": {
            "thread_id": thread_id,
            "checkpoint_ns": "main",
        }
    }
    if checkpoint_id is not None:
        cfg["configurable"]["checkpoint_id"] = checkpoint_id
    return cfg


# ---------------------------------------------------------------------------
# Existing SQLite tests (unchanged)
# ---------------------------------------------------------------------------


def test_resolve_checkpoint_db_path_relative(tmp_path: Path) -> None:
    out = resolve_checkpoint_db_path(repo_root=tmp_path, db_path="artifacts/checkpoints/a.sqlite")
    assert out == (tmp_path / "artifacts/checkpoints/a.sqlite").resolve()


def test_stable_checkpoint_thread_id_is_deterministic() -> None:
    a = stable_checkpoint_thread_id(request="fix bug", thread_prefix="lg-orch", provided=None)
    b = stable_checkpoint_thread_id(request="fix bug", thread_prefix="lg-orch", provided=None)
    assert a == b
    assert a.startswith("lg-orch-")


def test_stable_checkpoint_thread_id_uses_explicit_value() -> None:
    out = stable_checkpoint_thread_id(
        request="ignored",
        thread_prefix="lg-orch",
        provided="thread-explicit",
    )
    assert out == "thread-explicit"


def test_sqlite_checkpoint_persists_and_resume_from_latest(tmp_path: Path) -> None:
    db_path = tmp_path / "checkpoints.sqlite"
    saver = SqliteCheckpointSaver(db_path=db_path)
    app = build_graph(checkpointer=saver)

    run_config = {"configurable": {"thread_id": "thread-a", "checkpoint_ns": "main"}}

    first = app.invoke(_base_state(), config=run_config)
    assert "intent" in first

    latest = saver.get_tuple(run_config)
    assert latest is not None
    latest_cfg = latest.config.get("configurable", {})
    assert isinstance(latest_cfg, dict)
    checkpoint_id = latest_cfg.get("checkpoint_id")
    assert isinstance(checkpoint_id, str)
    assert checkpoint_id != ""

    saver_second = SqliteCheckpointSaver(db_path=db_path)
    resumed = build_graph(checkpointer=saver_second)
    resumed_out = resumed.invoke(
        _base_state(),
        config={
            "configurable": {
                "thread_id": "thread-a",
                "checkpoint_ns": "main",
                "checkpoint_id": checkpoint_id,
            }
        },
    )
    assert "final" in resumed_out


# ---------------------------------------------------------------------------
# Factory tests
# ---------------------------------------------------------------------------


def test_create_checkpoint_saver_factory_sqlite(tmp_path: Path) -> None:
    db_path = tmp_path / "ckpt.sqlite"
    saver = create_checkpoint_saver("sqlite", db_path=db_path)
    assert isinstance(saver, SqliteCheckpointSaver)


def test_create_checkpoint_saver_factory_redis() -> None:
    saver = create_checkpoint_saver(
        "redis",
        redis_url="redis://localhost:6379/0",
        ttl_seconds=3600,
    )
    assert isinstance(saver, RedisCheckpointSaver)


def test_create_checkpoint_saver_factory_postgres() -> None:
    pytest.importorskip("psycopg_pool", reason="postgres extra not installed")
    saver = create_checkpoint_saver("postgres", dsn="postgresql://user:pass@localhost/db")
    assert isinstance(saver, PostgresCheckpointSaver)


def test_create_checkpoint_saver_factory_unknown() -> None:
    with pytest.raises(ValueError, match="Unknown checkpoint backend"):
        create_checkpoint_saver("unknown_backend")


# ---------------------------------------------------------------------------
# Redis tests (use fakeredis so no real Redis instance is needed)
# ---------------------------------------------------------------------------


def _get_fake_redis_client() -> Any:
    """Return a fakeredis async client; skip if fakeredis is not installed."""
    try:
        import fakeredis.aioredis as fakeredis_aio  # type: ignore[import-untyped]

        return fakeredis_aio.FakeRedis()
    except ImportError:
        pytest.skip("fakeredis not installed")


def _make_redis_saver_with_fake_client() -> RedisCheckpointSaver:
    """Build a RedisCheckpointSaver whose internal client is replaced with a FakeRedis."""
    try:
        import fakeredis.aioredis as fakeredis_aio  # type: ignore[import-untyped]
    except ImportError:
        pytest.skip("fakeredis not installed")

    saver = create_checkpoint_saver(
        "redis",
        redis_url="redis://localhost:6379/15",
        ttl_seconds=60,
    )
    # Swap out the real client with FakeRedis so tests don't need a running Redis.
    assert isinstance(saver, RedisCheckpointSaver)
    saver._client = fakeredis_aio.FakeRedis()
    return saver


@pytest.mark.asyncio
async def test_redis_checkpoint_saver_put_get() -> None:
    """Round-trip a checkpoint through RedisCheckpointSaver using FakeRedis."""
    saver = _make_redis_saver_with_fake_client()
    thread_id = "redis-test-thread-1"
    checkpoint_id = "00000000-0000-0000-0000-000000000001"

    config = _make_fake_config(thread_id)
    checkpoint = _make_fake_checkpoint(checkpoint_id)
    metadata: dict[str, Any] = {"source": "input", "step": 0, "writes": {}, "parents": {}}
    new_versions: dict[str, Any] = {}

    new_cfg = await saver.aput(config, checkpoint, metadata, new_versions)  # type: ignore[arg-type]
    assert new_cfg["configurable"]["thread_id"] == thread_id
    assert new_cfg["configurable"]["checkpoint_id"] == checkpoint_id

    fetched = await saver.aget_tuple(config)
    assert fetched is not None
    assert fetched.checkpoint["id"] == checkpoint_id


@pytest.mark.asyncio
async def test_redis_checkpoint_saver_list() -> None:
    """alist should return checkpoints in insertion order (most recent first)."""
    saver = _make_redis_saver_with_fake_client()
    thread_id = "redis-test-thread-list"

    ids = [f"00000000-0000-0000-0000-00000000000{i}" for i in range(1, 4)]
    config = _make_fake_config(thread_id)
    metadata: dict[str, Any] = {"source": "input", "step": 0, "writes": {}, "parents": {}}

    for ckpt_id in ids:
        checkpoint = _make_fake_checkpoint(ckpt_id)
        await saver.aput(config, checkpoint, metadata, {})  # type: ignore[arg-type]
        # Small sleep to ensure monotonically increasing timestamps in the sorted set.
        await asyncio.sleep(0.01)

    collected: list[str] = []
    async for tup in saver.alist(config):
        collected.append(tup.checkpoint["id"])

    # Most recently inserted should come first.
    assert collected[0] == ids[-1]
    assert len(collected) == 3


@pytest.mark.asyncio
async def test_redis_checkpoint_saver_ttl() -> None:
    """Each written key must carry a positive TTL."""
    saver = _make_redis_saver_with_fake_client()
    thread_id = "redis-test-thread-ttl"
    checkpoint_id = "00000000-0000-0000-0000-000000000099"

    config = _make_fake_config(thread_id)
    checkpoint = _make_fake_checkpoint(checkpoint_id)
    metadata: dict[str, Any] = {"source": "input", "step": 0, "writes": {}, "parents": {}}

    await saver.aput(config, checkpoint, metadata, {})  # type: ignore[arg-type]

    ckpt_key = saver._ckpt_key(thread_id, "main", checkpoint_id)
    ttl = await saver._client.ttl(ckpt_key)
    assert ttl > 0, f"Expected positive TTL on checkpoint key, got {ttl}"

    idx_key = saver._idx_key(thread_id, "main")
    ttl_idx = await saver._client.ttl(idx_key)
    assert ttl_idx > 0, f"Expected positive TTL on index key, got {ttl_idx}"


# ---------------------------------------------------------------------------
# Postgres test (real Postgres, skipped unless POSTGRES_TEST_DSN is set)
# ---------------------------------------------------------------------------
# To run locally:
#   POSTGRES_TEST_DSN="postgresql://user:pass@localhost:5432/testdb" pytest py/tests/test_checkpointing.py::test_postgres_checkpoint_saver_put_get -v


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("POSTGRES_TEST_DSN"),
    reason="Set POSTGRES_TEST_DSN=postgresql://... to run this test against a real Postgres.",
)
async def test_postgres_checkpoint_saver_put_get() -> None:
    """Round-trip a checkpoint through PostgresCheckpointSaver (requires real Postgres)."""
    dsn = os.environ["POSTGRES_TEST_DSN"]
    table_name = "lula_checkpoints_test"
    saver = create_checkpoint_saver("postgres", dsn=dsn, table_name=table_name)
    assert isinstance(saver, PostgresCheckpointSaver)

    thread_id = "pg-test-thread-1"
    checkpoint_id = "00000000-0000-0000-0000-abcdef000001"
    config = _make_fake_config(thread_id)
    checkpoint = _make_fake_checkpoint(checkpoint_id)
    metadata: dict[str, Any] = {"source": "input", "step": 0, "writes": {}, "parents": {}}

    try:
        new_cfg = await saver.aput(config, checkpoint, metadata, {})  # type: ignore[arg-type]
        assert new_cfg["configurable"]["thread_id"] == thread_id
        assert new_cfg["configurable"]["checkpoint_id"] == checkpoint_id

        fetched = await saver.aget_tuple(config)
        assert fetched is not None
        assert fetched.checkpoint["id"] == checkpoint_id
    finally:
        await saver.adelete_thread(thread_id)
        await saver.aclose()


# ---------------------------------------------------------------------------
# RunStore namespace enforcement
# ---------------------------------------------------------------------------


def test_namespace_enforcement_in_run_store(tmp_path: Path) -> None:
    """list_runs(namespace=...) must not return runs from a different namespace."""
    db = tmp_path / "runs.sqlite"
    store = RunStore(db_path=db, namespace="ns1")

    now = "2024-01-01T00:00:00Z"
    run_ns1: dict[str, Any] = {
        "run_id": "run-ns1-001",
        "request": "do something",
        "status": "succeeded",
        "created_at": now,
        "started_at": now,
        "finished_at": now,
        "exit_code": 0,
        "trace_out_dir": "artifacts/runs",
        "trace_path": "artifacts/runs/run-ns1-001.json",
    }
    store.upsert(run_ns1)

    # Insert a run in a different namespace using a different store instance.
    store_ns2 = RunStore(db_path=db, namespace="ns2")
    run_ns2: dict[str, Any] = {
        "run_id": "run-ns2-001",
        "request": "do something else",
        "status": "succeeded",
        "created_at": now,
        "started_at": now,
        "finished_at": now,
        "exit_code": 0,
        "trace_out_dir": "artifacts/runs",
        "trace_path": "artifacts/runs/run-ns2-001.json",
    }
    store_ns2.upsert(run_ns2)
    store_ns2.close()

    # list_runs with explicit namespace should only return that namespace's runs.
    ns1_runs = store.list_runs(namespace="ns1")
    assert all(r["namespace"] == "ns1" for r in ns1_runs)
    assert any(r["run_id"] == "run-ns1-001" for r in ns1_runs)
    assert not any(r["run_id"] == "run-ns2-001" for r in ns1_runs)

    ns2_runs = store.list_runs(namespace="ns2")
    assert all(r["namespace"] == "ns2" for r in ns2_runs)
    assert any(r["run_id"] == "run-ns2-001" for r in ns2_runs)
    assert not any(r["run_id"] == "run-ns1-001" for r in ns2_runs)

    # get_run with wrong namespace should return None.
    assert store.get_run("run-ns2-001", namespace="ns1") is None
    assert store.get_run("run-ns1-001", namespace="ns2") is None

    store.close()
