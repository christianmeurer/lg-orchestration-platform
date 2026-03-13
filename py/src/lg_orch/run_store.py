from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_COLUMNS = (
    "run_id",
    "request",
    "status",
    "created_at",
    "started_at",
    "finished_at",
    "exit_code",
    "trace_out_dir",
    "trace_path",
    "request_id",
    "auth_subject",
    "client_ip",
    "namespace",
)

_CREATE_TABLE = """\
CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT PRIMARY KEY,
    request      TEXT NOT NULL,
    status       TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    exit_code    INTEGER,
    trace_out_dir TEXT NOT NULL,
    trace_path   TEXT NOT NULL,
    request_id   TEXT NOT NULL DEFAULT '',
    auth_subject TEXT NOT NULL DEFAULT '',
    client_ip    TEXT NOT NULL DEFAULT '',
    namespace    TEXT NOT NULL DEFAULT ''
)
"""

_CREATE_RECOVERY_FACTS_TABLE = """\
CREATE TABLE IF NOT EXISTS recovery_facts (
    fingerprint    TEXT NOT NULL,
    run_id         TEXT NOT NULL,
    loop           INTEGER NOT NULL DEFAULT 0,
    failure_class  TEXT NOT NULL DEFAULT '',
    summary        TEXT NOT NULL DEFAULT '',
    last_check     TEXT NOT NULL DEFAULT '',
    context_scope  TEXT NOT NULL DEFAULT '',
    retry_target   TEXT,
    plan_action    TEXT NOT NULL DEFAULT 'keep',
    salience       INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL,
    namespace      TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (fingerprint, run_id)
)
"""

_CREATE_INDEX_RUNS_NAMESPACE = (
    "CREATE INDEX IF NOT EXISTS idx_runs_namespace ON runs(namespace)"
)
_CREATE_INDEX_RECOVERY_FACTS_NAMESPACE = (
    "CREATE INDEX IF NOT EXISTS idx_recovery_facts_namespace ON recovery_facts(namespace)"
)

_RECOVERY_FACT_COLUMNS = (
    "fingerprint",
    "run_id",
    "loop",
    "failure_class",
    "summary",
    "last_check",
    "context_scope",
    "retry_target",
    "plan_action",
    "salience",
    "created_at",
    "namespace",
)


class RunStore:
    def __init__(self, *, db_path: Path, namespace: str = "") -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._namespace = namespace.strip()
        with self._lock:
            self._conn.execute(_CREATE_TABLE)
            self._conn.execute(_CREATE_RECOVERY_FACTS_TABLE)
            self._conn.execute(_CREATE_INDEX_RUNS_NAMESPACE)
            self._conn.execute(_CREATE_INDEX_RECOVERY_FACTS_NAMESPACE)
            self._conn.commit()
        self._migrate()

    def _migrate(self) -> None:
        for table in ("runs", "recovery_facts"):
            try:
                self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN namespace TEXT NOT NULL DEFAULT ''"
                )
                self._conn.commit()
            except sqlite3.OperationalError:
                pass  # Column already exists

    def upsert(self, record: dict[str, Any]) -> None:
        data = {k: record[k] for k in _COLUMNS if k in record}
        # Always inject namespace
        data["namespace"] = self._namespace
        if not data:
            return
        cols = ", ".join(data.keys())
        placeholders = ", ".join("?" for _ in data)
        sql = f"INSERT OR REPLACE INTO runs ({cols}) VALUES ({placeholders})"
        with self._lock:
            self._conn.execute(sql, list(data.values()))
            self._conn.commit()

    def list_runs(self) -> list[dict[str, Any]]:
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM runs WHERE namespace = ? ORDER BY created_at DESC",
                (self._namespace,),
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM runs WHERE run_id = ? AND namespace = ?",
                (run_id, self._namespace),
            )
            row = cursor.fetchone()
            return dict(row) if row is not None else None

    def upsert_recovery_facts(self, run_id: str, facts: list[dict[str, Any]]) -> None:
        """Persist recovery facts from a run. Only facts with a non-empty fingerprint are stored."""
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        rows: list[tuple[Any, ...]] = []
        for fact in facts:
            if not isinstance(fact, dict):
                continue
            fingerprint = str(fact.get("failure_fingerprint", "")).strip()
            if not fingerprint:
                continue
            loop_raw = fact.get("loop", 0)
            loop = loop_raw if isinstance(loop_raw, int) and not isinstance(loop_raw, bool) else 0
            salience_raw = fact.get("salience", 0)
            salience = (
                salience_raw
                if isinstance(salience_raw, int) and not isinstance(salience_raw, bool)
                else 0
            )
            retry_target = fact.get("retry_target")
            rows.append((
                fingerprint,
                run_id,
                loop,
                str(fact.get("failure_class", "")).strip(),
                str(fact.get("summary", fact.get("loop_summary", ""))).strip(),
                str(fact.get("last_check", "")).strip(),
                str(fact.get("context_scope", "")).strip(),
                str(retry_target).strip() if retry_target is not None else None,
                str(fact.get("plan_action", "keep")).strip() or "keep",
                salience,
                now,
                self._namespace,
            ))
        if not rows:
            return
        sql = (
            "INSERT OR REPLACE INTO recovery_facts "
            "(fingerprint, run_id, loop, failure_class, summary, last_check, "
            "context_scope, retry_target, plan_action, salience, created_at, namespace) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        with self._lock:
            self._conn.executemany(sql, rows)
            self._conn.commit()

    def get_recent_recovery_facts(
        self,
        *,
        fingerprint: str | None = None,
        failure_class: str | None = None,
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        """
        Return up to `limit` recent recovery facts.
        If fingerprint is given, filter to matching fingerprint first (exact match).
        If failure_class is given (and no fingerprint match found), filter by failure_class.
        Order by salience DESC, loop DESC.
        """
        limit = max(1, limit)
        with self._lock:
            if fingerprint:
                cursor = self._conn.execute(
                    "SELECT * FROM recovery_facts WHERE fingerprint = ? AND namespace = ? "
                    "ORDER BY salience DESC, loop DESC LIMIT ?",
                    (fingerprint, self._namespace, limit),
                )
                rows = [dict(row) for row in cursor.fetchall()]
                if rows:
                    return rows
            if failure_class:
                cursor = self._conn.execute(
                    "SELECT * FROM recovery_facts WHERE failure_class = ? AND namespace = ? "
                    "ORDER BY salience DESC, loop DESC LIMIT ?",
                    (failure_class, self._namespace, limit),
                )
                return [dict(row) for row in cursor.fetchall()]
            cursor = self._conn.execute(
                "SELECT * FROM recovery_facts WHERE namespace = ? "
                "ORDER BY salience DESC, loop DESC LIMIT ?",
                (self._namespace, limit),
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_episodic_context(
        self,
        *,
        failure_fingerprint: str = "",
        failure_class: str = "",
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """
        Return recent recovery facts relevant to the given fingerprint or class.
        Convenience wrapper around get_recent_recovery_facts.
        """
        return self.get_recent_recovery_facts(
            fingerprint=failure_fingerprint or None,
            failure_class=failure_class or None,
            limit=limit,
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()
