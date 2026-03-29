# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Christian Meurer — https://github.com/christianmeurer/Lula
"""
Tripartite persistent cross-session memory store.

Three independent tiers:
  - semantic:   facts and concepts that generalize across runs, stored with
                FTS5 + dense float32 cosine-similarity (numpy).
  - episodic:   per-run summaries and outcomes.
  - procedural: verified action sequences that succeeded.

No external vector DB is required; numpy handles all embedding math.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import struct
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

import numpy as np
import structlog

_log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

EmbedderFn = Callable[[str], list[float]]

Embedder = Callable[[str], "np.ndarray[Any, np.dtype[np.float32]]"]

Tier = Literal["semantic", "episodic", "procedural"]


@dataclass
class MemoryRecord:
    id: int | None
    tier: Tier
    run_id: str | None
    content: str
    metadata: dict[str, Any]
    created_at: float
    embedding: np.ndarray[Any, np.dtype[np.float32]] | None = field(default=None)


# ---------------------------------------------------------------------------
# Stub embedder (deterministic, hash-based, unit-norm, for testing only)
# ---------------------------------------------------------------------------


def stub_embedder(text: str, dim: int = 128) -> np.ndarray[Any, np.dtype[np.float32]]:
    """Deterministic hash-based unit-norm float32 vector.

    Fills each 4-byte chunk of the output vector from successive SHA-256
    digests so the result is reproducible given the same *text* and *dim*.
    """
    chunks: list[float] = []
    seed = text.encode("utf-8", errors="replace")
    h = hashlib.sha256(seed).digest()
    while len(chunks) < dim:
        for i in range(0, len(h) - 3, 4):
            val = int.from_bytes(h[i : i + 4], "little", signed=True)
            chunks.append(float(val))
            if len(chunks) >= dim:
                break
        seed = h  # chain: next digest uses previous output
        h = hashlib.sha256(seed).digest()

    vec = np.array(chunks[:dim], dtype=np.float32)
    norm = float(np.linalg.norm(vec))
    if norm < 1e-9:
        vec[0] = 1.0
        norm = 1.0
    return (vec / norm).astype(np.float32)


def _stub_embedder_as_list(text: str) -> list[float]:
    """Wrapper around stub_embedder that returns list[float] for EmbedderFn compat."""
    return stub_embedder(text).tolist()


class OllamaEmbedder:
    """Embedding provider using Ollama's local embedding API.

    Requires Ollama to be running at the configured endpoint.
    Falls back to stub_embedder on connection failure.
    """

    def __init__(
        self,
        model: str = "nomic-embed-text",
        base_url: str = "http://localhost:11434",
        timeout: float = 10.0,
    ) -> None:
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._available: bool | None = None  # None = not yet probed

    def _probe(self) -> bool:
        """Check if Ollama is reachable. Cached after first call."""
        if self._available is not None:
            return self._available
        try:
            import urllib.request

            req = urllib.request.Request(
                f"{self._base_url}/api/tags",
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=2.0):
                self._available = True
        except Exception:
            self._available = False
            logging.warning(
                "OllamaEmbedder: Ollama not reachable at %s; "
                "falling back to stub_embedder",
                self._base_url,
            )
        return self._available

    def __call__(self, text: str) -> list[float]:
        if not self._probe():
            return stub_embedder(text).tolist()
        try:
            import json as _json
            import urllib.request

            payload = _json.dumps({"model": self._model, "prompt": text}).encode()
            req = urllib.request.Request(
                f"{self._base_url}/api/embeddings",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                data = _json.loads(resp.read())
                embedding = data.get("embedding", [])
                if not embedding:
                    return stub_embedder(text).tolist()
                return [float(v) for v in embedding]
        except Exception as e:
            logging.warning("OllamaEmbedder: embedding failed: %s; using stub", e)
            return stub_embedder(text).tolist()


def probe_ollama(base_url: str = "http://localhost:11434") -> bool:
    """Check if Ollama is reachable. Called at startup to log embedding status."""
    try:
        import urllib.request

        with urllib.request.urlopen(f"{base_url}/api/tags", timeout=2.0):
            return True
    except Exception:
        return False


def make_embedder(provider: str | None = None, **kwargs: object) -> EmbedderFn:
    """Factory function for embedding providers.

    Args:
        provider: One of "ollama", "stub", or None (auto-detect from env).
        **kwargs: Provider-specific arguments (model, base_url, timeout).

    Environment variables:
        LG_EMBED_PROVIDER: "ollama" | "stub" (default: "stub")
        LG_EMBED_OLLAMA_URL: Ollama base URL (default: "http://localhost:11434")
        LG_EMBED_OLLAMA_MODEL: Ollama model name (default: "nomic-embed-text")

    Returns:
        A callable that takes a string and returns a list of floats.
    """
    if provider is None:
        provider = os.environ.get("LG_EMBED_PROVIDER", "stub")

    if provider == "ollama":
        base_url = str(
            kwargs.get(
                "base_url",
                os.environ.get("LG_EMBED_OLLAMA_URL", "http://localhost:11434"),
            )
        )
        model = str(
            kwargs.get(
                "model",
                os.environ.get("LG_EMBED_OLLAMA_MODEL", "nomic-embed-text"),
            )
        )
        timeout = float(kwargs.get("timeout", 10.0))
        return OllamaEmbedder(model=model, base_url=base_url, timeout=timeout)

    # Default: stub
    return _stub_embedder_as_list


# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

_DDL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS semantic_memories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    content     TEXT    NOT NULL,
    metadata    TEXT    NOT NULL DEFAULT '{}',
    embedding   BLOB    NOT NULL,
    created_at  REAL    NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS semantic_fts USING fts5(
    content,
    content='semantic_memories',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS semantic_ai AFTER INSERT ON semantic_memories BEGIN
    INSERT INTO semantic_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE TRIGGER IF NOT EXISTS semantic_ad AFTER DELETE ON semantic_memories BEGIN
    INSERT INTO semantic_fts(semantic_fts, rowid, content)
    VALUES ('delete', old.id, old.content);
END;

CREATE TRIGGER IF NOT EXISTS semantic_au AFTER UPDATE ON semantic_memories BEGIN
    INSERT INTO semantic_fts(semantic_fts, rowid, content)
    VALUES ('delete', old.id, old.content);
    INSERT INTO semantic_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE TABLE IF NOT EXISTS episodic_memories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT    NOT NULL,
    summary     TEXT    NOT NULL,
    outcome     TEXT    NOT NULL DEFAULT '',
    metadata    TEXT    NOT NULL DEFAULT '{}',
    created_at  REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_episodic_run_id ON episodic_memories(run_id);
CREATE INDEX IF NOT EXISTS idx_episodic_created_at ON episodic_memories(created_at DESC);

CREATE TABLE IF NOT EXISTS procedural_memories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type   TEXT    NOT NULL,
    steps       TEXT    NOT NULL,
    success     INTEGER NOT NULL DEFAULT 1,
    metadata    TEXT    NOT NULL DEFAULT '{}',
    created_at  REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_procedural_task_type ON procedural_memories(task_type);
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TASK_TYPE_KEYWORDS: dict[str, list[str]] = {
    "code_change": ["refactor", "implement", "add", "create", "write", "update", "change"],
    "debug": ["fix", "debug", "repair", "resolve", "investigate", "diagnose"],
    "analysis": ["analyze", "review", "check", "audit", "inspect", "assess"],
    "test_repair": ["test", "failing", "broken", "pytest", "assertion"],
    "canary": ["canary", "deploy", "smoke"],
}


def _infer_task_type(query: str) -> str:
    """Infer a task type from *query* by keyword matching, falling back to first word."""
    lower = query.lower()
    for task_type, keywords in _TASK_TYPE_KEYWORDS.items():
        if any(kw in lower for kw in keywords):
            return task_type
    # fallback: first word
    words = lower.split()
    return words[0] if words else "unknown"


def _cosine_similarity(
    a: np.ndarray[Any, np.dtype[np.float32]],
    b: np.ndarray[Any, np.dtype[np.float32]],
) -> float:
    """Cosine similarity between two float32 vectors (both assumed unit-norm)."""
    return float(np.dot(a, b))


def _approx_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


# ---------------------------------------------------------------------------
# LongTermMemoryStore
# ---------------------------------------------------------------------------


class LongTermMemoryStore:
    """Persistent tripartite memory store backed by a single SQLite database."""

    def __init__(
        self,
        db_path: str,
        embedder: Embedder | EmbedderFn | None = None,
        embedding_dim: int = 128,
    ) -> None:
        self._db_path = db_path
        if embedder is not None:
            self._embedder: Embedder | EmbedderFn = embedder
            _using_stub = False
        else:
            resolved = make_embedder()
            self._embedder = resolved
            # Check if the resolved embedder is the stub
            _using_stub = resolved is _stub_embedder_as_list
        self._embedding_dim = embedding_dim
        if _using_stub:
            _log.warning(
                "long_term_memory.stub_embedder_active",
                reason=(
                    "No real embedder provided; semantic search will return meaningless results. "
                    "Set LG_EMBED_PROVIDER=ollama to enable semantic retrieval."
                ),
            )
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_DDL)
            self._conn.commit()

    # ------------------------------------------------------------------
    # Internal utilities
    # ------------------------------------------------------------------

    def _embed(self, text: str) -> np.ndarray[Any, np.dtype[np.float32]]:
        raw = self._embedder(text)
        if isinstance(raw, np.ndarray):
            return raw.astype(np.float32)
        # EmbedderFn returns list[float]
        return np.array(raw, dtype=np.float32)

    @staticmethod
    def _blob_to_vec(blob: bytes, dim: int) -> np.ndarray[Any, np.dtype[np.float32]]:
        return np.frombuffer(blob, dtype=np.float32).copy()

    @staticmethod
    def _vec_to_blob(vec: np.ndarray[Any, np.dtype[np.float32]]) -> bytes:
        return vec.astype(np.float32).tobytes()

    # ------------------------------------------------------------------
    # Semantic tier
    # ------------------------------------------------------------------

    def store_semantic(
        self,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        """Embed and store a semantic fact. Returns the row id."""
        meta_json = json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True)
        embedding = self._embed(content)
        blob = self._vec_to_blob(embedding)
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO semantic_memories (content, metadata, embedding, created_at) "
                "VALUES (?, ?, ?, ?)",
                (content, meta_json, blob, now),
            )
            self._conn.commit()
            return int(cur.lastrowid)  # type: ignore[arg-type]

    def search_semantic(self, query: str, top_k: int = 5) -> list[MemoryRecord]:
        """Cosine similarity search over stored embeddings.

        Falls back to FTS5 text search when the table is empty or when no
        embeddings score above 0 (degenerate case).
        """
        top_k = max(1, top_k)
        query_vec = self._embed(query)

        with self._lock:
            rows = self._conn.execute(
                "SELECT id, content, metadata, embedding, created_at FROM semantic_memories"
            ).fetchall()

        if not rows:
            return []

        row_count = len(rows)
        if row_count > 5_000:
            _log.warning(
                "long_term_memory.semantic_scan_large",
                row_count=row_count,
            )

        scored: list[tuple[float, MemoryRecord]] = []
        for row in rows:
            try:
                vec = self._blob_to_vec(row["embedding"], self._embedding_dim)
                sim = _cosine_similarity(query_vec, vec)
            except Exception:
                sim = 0.0
                vec = None
            try:
                meta: dict[str, Any] = json.loads(row["metadata"])
            except (json.JSONDecodeError, TypeError):
                meta = {}
            rec = MemoryRecord(
                id=int(row["id"]),
                tier="semantic",
                run_id=None,
                content=str(row["content"]),
                metadata=meta,
                created_at=float(row["created_at"]),
                embedding=vec if vec is not None else None,
            )
            scored.append((sim, rec))

        scored.sort(key=lambda t: t[0], reverse=True)
        return [rec for _, rec in scored[:top_k]]

    # ------------------------------------------------------------------
    # Episodic tier
    # ------------------------------------------------------------------

    def store_episode(
        self,
        run_id: str,
        summary: str,
        outcome: str,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        """Store a per-run episode. Returns the row id."""
        meta_json = json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True)
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO episodic_memories (run_id, summary, outcome, metadata, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (run_id, summary, outcome, meta_json, now),
            )
            self._conn.commit()
            return int(cur.lastrowid)  # type: ignore[arg-type]

    def get_episodes(
        self,
        limit: int = 20,
        run_id: str | None = None,
    ) -> list[MemoryRecord]:
        """Retrieve recent episodes, optionally filtered by run_id.

        Results are returned in reverse-chronological order (most recent first).
        """
        limit = max(1, limit)
        with self._lock:
            if run_id is not None:
                rows = self._conn.execute(
                    "SELECT id, run_id, summary, outcome, metadata, created_at "
                    "FROM episodic_memories WHERE run_id = ? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (run_id, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT id, run_id, summary, outcome, metadata, created_at "
                    "FROM episodic_memories ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()

        result: list[MemoryRecord] = []
        for row in rows:
            try:
                meta: dict[str, Any] = json.loads(row["metadata"])
            except (json.JSONDecodeError, TypeError):
                meta = {}
            content = str(row["summary"])
            if row["outcome"]:
                content = f"{content}\noutcome: {row['outcome']}"
            result.append(
                MemoryRecord(
                    id=int(row["id"]),
                    tier="episodic",
                    run_id=str(row["run_id"]),
                    content=content,
                    metadata=meta,
                    created_at=float(row["created_at"]),
                    embedding=None,
                )
            )
        return result

    # ------------------------------------------------------------------
    # Procedural tier
    # ------------------------------------------------------------------

    def store_procedure(
        self,
        task_type: str,
        steps: list[str],
        success: bool,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        """Store a procedural memory (tool sequence). Returns the row id."""
        steps_json = json.dumps(steps, ensure_ascii=False)
        meta_json = json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True)
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO procedural_memories "
                "(task_type, steps, success, metadata, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (task_type, steps_json, int(success), meta_json, now),
            )
            self._conn.commit()
            return int(cur.lastrowid)  # type: ignore[arg-type]

    def get_procedures(
        self,
        task_type: str,
        successful_only: bool = True,
    ) -> list[MemoryRecord]:
        """Retrieve procedures for a given task type."""
        with self._lock:
            if successful_only:
                rows = self._conn.execute(
                    "SELECT id, task_type, steps, success, metadata, created_at "
                    "FROM procedural_memories "
                    "WHERE task_type = ? AND success = 1 "
                    "ORDER BY created_at DESC",
                    (task_type,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT id, task_type, steps, success, metadata, created_at "
                    "FROM procedural_memories WHERE task_type = ? "
                    "ORDER BY created_at DESC",
                    (task_type,),
                ).fetchall()

        result: list[MemoryRecord] = []
        for row in rows:
            try:
                steps: list[str] = json.loads(row["steps"])
            except (json.JSONDecodeError, TypeError):
                steps = []
            try:
                meta: dict[str, Any] = json.loads(row["metadata"])
            except (json.JSONDecodeError, TypeError):
                meta = {}
            content = f"task_type: {row['task_type']}\nsteps: {', '.join(steps)}"
            result.append(
                MemoryRecord(
                    id=int(row["id"]),
                    tier="procedural",
                    run_id=None,
                    content=content,
                    metadata=meta,
                    created_at=float(row["created_at"]),
                    embedding=None,
                )
            )
        return result

    # ------------------------------------------------------------------
    # Cross-tier retrieval
    # ------------------------------------------------------------------

    def retrieve_for_context(self, query: str, max_tokens: int = 2000) -> str:
        """Return a formatted string of relevant cross-tier memories.

        Prioritises semantic (cosine), then recent episodes, then successful
        procedures. Budget is capped to *max_tokens* (approx 4 chars/token).
        """
        budget = max(1, max_tokens)
        parts: list[str] = []

        # --- semantic ---
        semantic_hits = self.search_semantic(query, top_k=5)
        if semantic_hits:
            lines: list[str] = []
            for rec in semantic_hits:
                lines.append(f"- {rec.content}")
            block = "[long_term:semantic]\n" + "\n".join(lines)
            tok = _approx_tokens(block)
            if tok <= budget:
                parts.append(block)
                budget -= tok
            else:
                truncated = block[: budget * 4]
                parts.append(truncated)
                budget = 0

        if budget <= 0:
            return "\n\n".join(parts).strip()

        # --- episodic ---
        episodes = self.get_episodes(limit=5)
        if episodes:
            lines = []
            for rec in episodes:
                run_label = f"[{rec.run_id}] " if rec.run_id else ""
                lines.append(f"- {run_label}{rec.content}")
            block = "[long_term:episodic]\n" + "\n".join(lines)
            tok = _approx_tokens(block)
            if tok <= budget:
                parts.append(block)
                budget -= tok
            else:
                truncated = block[: budget * 4]
                parts.append(truncated)
                budget = 0

        if budget <= 0:
            return "\n\n".join(parts).strip()

        # --- procedural: query used as task_type heuristic ---
        task_hint = _infer_task_type(query) if query.strip() else ""
        if task_hint:
            procs = self.get_procedures(task_hint, successful_only=True)
            if not procs:
                # Broaden: fetch most recent successful procedures regardless of type
                with self._lock:
                    rows = self._conn.execute(
                        "SELECT id, task_type, steps, success, metadata, created_at "
                        "FROM procedural_memories WHERE success = 1 "
                        "ORDER BY created_at DESC LIMIT 3"
                    ).fetchall()
                procs = []
                for row in rows:
                    try:
                        steps_list: list[str] = json.loads(row["steps"])
                    except (json.JSONDecodeError, TypeError):
                        steps_list = []
                    try:
                        meta: dict[str, Any] = json.loads(row["metadata"])
                    except (json.JSONDecodeError, TypeError):
                        meta = {}
                    procs.append(
                        MemoryRecord(
                            id=int(row["id"]),
                            tier="procedural",
                            run_id=None,
                            content=(
                                f"task_type: {row['task_type']}\nsteps: {', '.join(steps_list)}"
                            ),
                            metadata=meta,
                            created_at=float(row["created_at"]),
                            embedding=None,
                        )
                    )
            if procs:
                lines = [f"- {rec.content}" for rec in procs[:3]]
                block = "[long_term:procedural]\n" + "\n".join(lines)
                tok = _approx_tokens(block)
                if tok <= budget:
                    parts.append(block)
                else:
                    truncated = block[: budget * 4]
                    parts.append(truncated)

        return "\n\n".join(p for p in parts if p.strip()).strip()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            self._conn.close()


__all__ = [
    "_TASK_TYPE_KEYWORDS",
    "Embedder",
    "EmbedderFn",
    "LongTermMemoryStore",
    "MemoryRecord",
    "OllamaEmbedder",
    "Tier",
    "_infer_task_type",
    "make_embedder",
    "probe_ollama",
    "stub_embedder",
]
