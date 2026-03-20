from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

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
    "thread_id",
    "checkpoint_id",
    "pending_approval",
    "pending_approval_summary",
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
    thread_id    TEXT NOT NULL DEFAULT '',
    checkpoint_id TEXT NOT NULL DEFAULT '',
    pending_approval INTEGER NOT NULL DEFAULT 0,
    pending_approval_summary TEXT NOT NULL DEFAULT '',
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

_CREATE_SEMANTIC_MEMORIES_TABLE = """\
CREATE TABLE IF NOT EXISTS semantic_memories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_key  TEXT NOT NULL,
    run_id      TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT '',
    summary     TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    namespace   TEXT NOT NULL DEFAULT '',
    UNIQUE(memory_key, run_id, namespace)
)
"""

_CREATE_INDEX_SEMANTIC_MEMORIES_NAMESPACE = (
    "CREATE INDEX IF NOT EXISTS idx_semantic_memories_namespace ON semantic_memories(namespace)"
)

_CREATE_SEMANTIC_MEMORIES_FTS = """\
CREATE VIRTUAL TABLE IF NOT EXISTS semantic_memories_fts USING fts5(
    summary,
    kind,
    source,
    content='semantic_memories',
    content_rowid='id'
)
"""

_CREATE_RUNS_FTS = """\
CREATE VIRTUAL TABLE IF NOT EXISTS runs_fts USING fts5(
    run_id,
    request,
    status,
    summary,
    content='runs'
)
"""

_CREATE_RUNS_FTS_INSERT_TRIGGER = """\
CREATE TRIGGER IF NOT EXISTS runs_fts_ai AFTER INSERT ON runs BEGIN
    INSERT INTO runs_fts(rowid, run_id, request, status, summary)
    VALUES (new.rowid, new.run_id, new.request, new.status, new.pending_approval_summary);
END
"""

_CREATE_RUNS_FTS_UPDATE_TRIGGER = """\
CREATE TRIGGER IF NOT EXISTS runs_fts_au AFTER UPDATE ON runs BEGIN
    INSERT INTO runs_fts(runs_fts, rowid, run_id, request, status, summary)
    VALUES ('delete', old.rowid, old.run_id, old.request, old.status, old.pending_approval_summary);
    INSERT INTO runs_fts(rowid, run_id, request, status, summary)
    VALUES (new.rowid, new.run_id, new.request, new.status, new.pending_approval_summary);
END
"""

_CREATE_RUNS_FTS_DELETE_TRIGGER = """\
CREATE TRIGGER IF NOT EXISTS runs_fts_ad AFTER DELETE ON runs BEGIN
    INSERT INTO runs_fts(runs_fts, rowid, run_id, request, status, summary)
    VALUES ('delete', old.rowid, old.run_id, old.request, old.status, old.pending_approval_summary);
END
"""

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
        # WAL mode: concurrent readers don't block writers; NORMAL sync is safe
        # for WAL and avoids fsync on every commit. busy_timeout prevents
        # immediate SQLITE_BUSY failures under concurrent access.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._lock = threading.Lock()
        self._namespace = namespace.strip()
        self._fts_enabled = False
        self._runs_fts_enabled = False
        with self._lock:
            self._conn.execute(_CREATE_TABLE)
            self._conn.execute(_CREATE_RECOVERY_FACTS_TABLE)
            self._conn.execute(_CREATE_INDEX_RUNS_NAMESPACE)
            self._conn.execute(_CREATE_INDEX_RECOVERY_FACTS_NAMESPACE)
            self._conn.execute(_CREATE_SEMANTIC_MEMORIES_TABLE)
            self._conn.execute(_CREATE_INDEX_SEMANTIC_MEMORIES_NAMESPACE)
            self._conn.commit()
        self._migrate()
        self._ensure_semantic_fts()
        self._ensure_runs_fts()

    def _migrate(self) -> None:
        run_columns = (
            ("namespace", "TEXT NOT NULL DEFAULT ''"),
            ("thread_id", "TEXT NOT NULL DEFAULT ''"),
            ("checkpoint_id", "TEXT NOT NULL DEFAULT ''"),
            ("pending_approval", "INTEGER NOT NULL DEFAULT 0"),
            ("pending_approval_summary", "TEXT NOT NULL DEFAULT ''"),
        )
        for column, spec in run_columns:
            try:
                self._conn.execute(f"ALTER TABLE runs ADD COLUMN {column} {spec}")
                self._conn.commit()
            except sqlite3.OperationalError:
                pass

        try:
            self._conn.execute(
                "ALTER TABLE recovery_facts ADD COLUMN namespace TEXT NOT NULL DEFAULT ''"
            )
            self._conn.commit()
        except sqlite3.OperationalError:
            pass

        try:
            self._conn.execute(
                "ALTER TABLE semantic_memories ADD COLUMN namespace TEXT NOT NULL DEFAULT ''"
            )
            self._conn.commit()
        except sqlite3.OperationalError:
            pass

    def _ensure_runs_fts(self) -> None:
        try:
            with self._lock:
                self._conn.execute(_CREATE_RUNS_FTS)
                self._conn.execute(_CREATE_RUNS_FTS_INSERT_TRIGGER)
                self._conn.execute(_CREATE_RUNS_FTS_UPDATE_TRIGGER)
                self._conn.execute(_CREATE_RUNS_FTS_DELETE_TRIGGER)
                self._conn.commit()
            self._runs_fts_enabled = True
        except sqlite3.OperationalError:
            self._runs_fts_enabled = False

    def _ensure_semantic_fts(self) -> None:
        try:
            with self._lock:
                self._conn.execute(_CREATE_SEMANTIC_MEMORIES_FTS)
                self._conn.execute("INSERT INTO semantic_memories_fts(semantic_memories_fts) VALUES('rebuild')")
                self._conn.commit()
            self._fts_enabled = True
        except sqlite3.OperationalError:
            self._fts_enabled = False

    def _rebuild_semantic_fts(self) -> None:
        if not self._fts_enabled:
            return
        with self._lock:
            self._conn.execute("INSERT INTO semantic_memories_fts(semantic_memories_fts) VALUES('rebuild')")
            self._conn.commit()

    def search_runs(
        self,
        query: str,
        limit: int = 50,
        namespace: str | None = None,
    ) -> list[dict[str, Any]]:
        """Search runs using FTS5 full-text search (BM25 ranking).

        Falls back to a LIKE scan if FTS5 is unavailable.
        Invalid FTS5 syntax is caught and an empty list is returned.

        Parameters
        ----------
        query:
            Full-text search query string.
        limit:
            Maximum number of results to return.
        namespace:
            Optional namespace override. Defaults to the store's configured
            namespace when ``None``.
        """
        ns = namespace if namespace is not None else self._namespace
        query_text = query.strip()
        if not query_text:
            return []
        limit = max(1, limit)
        if self._runs_fts_enabled:
            try:
                with self._lock:
                    cursor = self._conn.execute(
                        "SELECT r.* FROM runs_fts "
                        "JOIN runs r ON r.rowid = runs_fts.rowid "
                        "WHERE runs_fts MATCH ? AND r.namespace = ? "
                        "ORDER BY rank LIMIT ?",
                        (query_text, ns, limit),
                    )
                    return [dict(row) for row in cursor.fetchall()]
            except sqlite3.OperationalError as exc:
                _log.warning(
                    "search_runs_fts_error",
                    extra={"query": query_text, "error": str(exc)},
                )
                return []

        like = f"%{query_text}%"
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM runs WHERE namespace = ? "
                "AND (run_id LIKE ? OR request LIKE ? OR status LIKE ? "
                "OR pending_approval_summary LIKE ?) "
                "ORDER BY created_at DESC LIMIT ?",
                (ns, like, like, like, like, limit),
            )
            return [dict(row) for row in cursor.fetchall()]

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

    def list_runs(self, namespace: str | None = None) -> list[dict[str, Any]]:
        """Return all runs for a namespace, ordered by created_at DESC.

        Parameters
        ----------
        namespace:
            Optional namespace override. Defaults to the store's configured
            namespace when ``None``.
        """
        ns = namespace if namespace is not None else self._namespace
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM runs WHERE namespace = ? ORDER BY created_at DESC",
                (ns,),
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_run(self, run_id: str, namespace: str | None = None) -> dict[str, Any] | None:
        """Fetch a single run by ID within a namespace.

        Parameters
        ----------
        run_id:
            The run identifier to look up.
        namespace:
            Optional namespace override. Defaults to the store's configured
            namespace when ``None``.
        """
        ns = namespace if namespace is not None else self._namespace
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM runs WHERE run_id = ? AND namespace = ?",
                (run_id, ns),
            )
            row = cursor.fetchone()
            return dict(row) if row is not None else None

    def upsert_recovery_facts(self, run_id: str, facts: list[dict[str, Any]]) -> None:
        """Persist recovery facts from a run. Only facts with a non-empty fingerprint are stored."""
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        rows: list[tuple[Any, ...]] = []
        for fact in facts:
            if not isinstance(fact, dict):
                continue  # type: ignore[unreachable]
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

    def upsert_semantic_memories(self, run_id: str, memories: list[dict[str, Any]]) -> None:
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        rows: list[tuple[Any, ...]] = []
        for memory in memories:
            if not isinstance(memory, dict):
                continue  # type: ignore[unreachable]
            summary = str(memory.get("summary", "")).strip()
            if not summary:
                continue
            memory_key = str(memory.get("memory_key", "")).strip()
            if not memory_key:
                memory_key = f"{str(memory.get('kind', '')).strip()}|{str(memory.get('source', '')).strip()}|{summary}".lower()
            rows.append(
                (
                    memory_key,
                    run_id,
                    str(memory.get("kind", "")).strip() or "run_note",
                    str(memory.get("source", "")).strip(),
                    summary,
                    now,
                    self._namespace,
                )
            )
        if not rows:
            return
        sql = (
            "INSERT OR REPLACE INTO semantic_memories "
            "(memory_key, run_id, kind, source, summary, created_at, namespace) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)"
        )
        with self._lock:
            self._conn.executemany(sql, rows)
            self._conn.commit()
        self._rebuild_semantic_fts()

    def search_semantic_memories(
        self,
        *,
        query: str,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        query_text = query.strip()
        if not query_text:
            return []
        limit = max(1, limit)
        if self._fts_enabled:
            with self._lock:
                cursor = self._conn.execute(
                    "SELECT m.run_id, m.kind, m.source, m.summary, m.created_at, "
                    "bm25(semantic_memories_fts) AS score "
                    "FROM semantic_memories_fts "
                    "JOIN semantic_memories m ON m.id = semantic_memories_fts.rowid "
                    "WHERE semantic_memories_fts MATCH ? AND m.namespace = ? "
                    "ORDER BY score ASC, m.created_at DESC LIMIT ?",
                    (query_text, self._namespace, limit),
                )
                return [dict(row) for row in cursor.fetchall()]

        like = f"%{query_text}%"
        with self._lock:
            cursor = self._conn.execute(
                "SELECT run_id, kind, source, summary, created_at, 0.0 AS score "
                "FROM semantic_memories WHERE namespace = ? "
                "AND (summary LIKE ? OR source LIKE ? OR kind LIKE ?) "
                "ORDER BY created_at DESC LIMIT ?",
                (self._namespace, like, like, like, limit),
            )
            return [dict(row) for row in cursor.fetchall()]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
