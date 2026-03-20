# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Christian Meurer — https://github.com/christianmeurer/Lula
"""
Procedural memory cache backed by SQLite.

Stores verified tool sequences (procedures) so the planner can retrieve
and reuse them without full LLM re-planning for routine operations.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from hashlib import sha256
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS procedures (
    procedure_id TEXT PRIMARY KEY,
    canonical_name TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    task_class TEXT NOT NULL DEFAULT '',
    steps_json TEXT NOT NULL,
    verification_json TEXT NOT NULL,
    use_count INTEGER NOT NULL DEFAULT 0,
    last_used_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_procedures_request_hash ON procedures(request_hash);
CREATE INDEX IF NOT EXISTS idx_procedures_canonical_name ON procedures(canonical_name);
"""


def _canonical_request_hash(request: str) -> str:
    """Deterministic hash of a lowercased, whitespace-normalized request."""
    normalized = " ".join(request.lower().split())
    return sha256(normalized.encode("utf-8")).hexdigest()[:32]


def _procedure_id(canonical_name: str, request_hash: str) -> str:
    seed = f"{canonical_name}|{request_hash}"
    return sha256(seed.encode("utf-8")).hexdigest()[:24]


class ProcedureCache:
    def __init__(self, *, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def store_procedure(
        self,
        *,
        canonical_name: str,
        request: str,
        task_class: str,
        steps: list[dict[str, Any]],
        verification: list[dict[str, Any]],
        created_at: str,
    ) -> str:
        """
        Store a verified procedure. Returns the procedure_id.
        Upserts on conflict (same procedure_id).
        """
        request_hash = _canonical_request_hash(request)
        procedure_id = _procedure_id(canonical_name, request_hash)
        steps_json = json.dumps(steps, ensure_ascii=False, sort_keys=True)
        verification_json = json.dumps(verification, ensure_ascii=False, sort_keys=True)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO procedures
                  (procedure_id, canonical_name, request_hash, task_class,
                   steps_json, verification_json, use_count, last_used_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 0, NULL, ?)
                ON CONFLICT(procedure_id) DO UPDATE SET
                  steps_json=excluded.steps_json,
                  verification_json=excluded.verification_json,
                  task_class=excluded.task_class,
                  created_at=excluded.created_at
                """,
                (procedure_id, canonical_name, request_hash, task_class,
                 steps_json, verification_json, created_at),
            )
            self._conn.commit()
        return procedure_id

    def lookup_procedure(
        self,
        *,
        request: str,
        canonical_name: str = "",
        limit: int = 3,
    ) -> list[dict[str, Any]]:
        """
        Look up cached procedures matching the request hash.
        If canonical_name is given, filter to that name first.
        Returns list of dicts with keys: procedure_id, canonical_name, task_class,
          steps, verification, use_count, last_used_at, created_at.
        Ordered by use_count DESC.
        """
        request_hash = _canonical_request_hash(request)
        with self._lock:
            if canonical_name:
                cursor = self._conn.execute(
                    """
                    SELECT * FROM procedures
                    WHERE request_hash = ? AND canonical_name = ?
                    ORDER BY use_count DESC
                    LIMIT ?
                    """,
                    (request_hash, canonical_name, limit),
                )
            else:
                cursor = self._conn.execute(
                    """
                    SELECT * FROM procedures
                    WHERE request_hash = ?
                    ORDER BY use_count DESC
                    LIMIT ?
                    """,
                    (request_hash, limit),
                )
            rows = cursor.fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                steps = json.loads(row["steps_json"])
                verification = json.loads(row["verification_json"])
            except (json.JSONDecodeError, KeyError):
                continue
            result.append({
                "procedure_id": row["procedure_id"],
                "canonical_name": row["canonical_name"],
                "task_class": row["task_class"],
                "steps": steps,
                "verification": verification,
                "use_count": row["use_count"],
                "last_used_at": row["last_used_at"],
                "created_at": row["created_at"],
            })
        return result

    def record_use(self, procedure_id: str, *, used_at: str) -> None:
        """Increment use_count and update last_used_at."""
        with self._lock:
            self._conn.execute(
                """
                UPDATE procedures
                SET use_count = use_count + 1, last_used_at = ?
                WHERE procedure_id = ?
                """,
                (used_at, procedure_id),
            )
            self._conn.commit()

    def list_procedures(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """Return most-used procedures for inspection."""
        with self._lock:
            cursor = self._conn.execute(
                "SELECT procedure_id, canonical_name, task_class, use_count, last_used_at, created_at "
                "FROM procedures ORDER BY use_count DESC LIMIT ?",
                (limit,),
            )
            rows = cursor.fetchall()
        return [dict(row) for row in rows]


def _canonical_procedure_name(steps: list[dict[str, Any]]) -> str:
    """Derive a canonical name from the tool names used in plan steps."""
    tool_names: list[str] = []
    for step in steps:
        if not isinstance(step, dict):
            continue  # type: ignore[unreachable]
        for tool_call in step.get("tools", []):
            if isinstance(tool_call, dict):
                name = str(tool_call.get("tool", "")).strip()
                if name and name not in tool_names:
                    tool_names.append(name)
    if not tool_names:
        return "unnamed_procedure"
    return "_".join(tool_names[:4])
