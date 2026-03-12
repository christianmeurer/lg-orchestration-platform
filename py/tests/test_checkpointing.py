from __future__ import annotations

from pathlib import Path

from lg_orch.checkpointing import (
    SqliteCheckpointSaver,
    resolve_checkpoint_db_path,
    stable_checkpoint_thread_id,
)
from lg_orch.graph import build_graph


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

