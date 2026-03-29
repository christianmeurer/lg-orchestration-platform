# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Christian Meurer — https://github.com/christianmeurer/Lula
"""SCIP symbol-index reader (Wave 9 - Cross-Repository Microservice Orchestration).

Reads a ``scip_index.json`` sidecar file from a repository root.  No protobuf
dependency is required; the module works entirely with the stdlib ``json`` and
``pathlib`` modules.

Sidecar JSON format
-------------------
.. code-block:: json

    {
        "documents": [
            {
                "relative_path": "src/foo.py",
                "symbols": [
                    {
                        "name": "MyClass",
                        "kind": "class",
                        "start_line": 10,
                        "end_line": 40,
                        "references": ["src/bar.py:use_my_class"]
                    }
                ]
            }
        ]
    }

Exported public names:
    ScipSymbol, ScipIndex, load_scip_index
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

__all__ = [
    "ScipIndex",
    "ScipSymbol",
    "load_scip_index",
]

_SIDECAR_FILENAME = "scip_index.json"


@dataclass
class ScipSymbol:
    """A single symbol entry from a SCIP index."""

    name: str
    kind: str  # "function", "class", "variable", "type"
    file_path: str  # relative to repo root
    start_line: int
    end_line: int
    references: list[str]  # "other/path.py:symbol_name" strings


@dataclass
class ScipIndex:
    """In-memory representation of a SCIP symbol index for one repository."""

    repo_root: str
    symbols: list[ScipSymbol] = field(default_factory=list)
    _stale: bool = field(default=False, repr=False)

    @property
    def is_stale(self) -> bool:
        """True if the index may be out of date due to file changes."""
        return self._stale

    def mark_stale(self) -> None:
        """Mark the index as stale. Called after apply_patch operations."""
        self._stale = True

    # ------------------------------------------------------------------
    # Query methods
    # ------------------------------------------------------------------

    def find_symbol(self, name: str) -> list[ScipSymbol]:
        """Return all symbols whose ``name`` equals *name* (exact match)."""
        if self._stale:
            import logging
            logging.debug("ScipIndex.find_symbol called on stale index; results may be outdated")
        return [s for s in self.symbols if s.name == name]

    def find_references(self, symbol_name: str) -> list[ScipSymbol]:
        """Return all symbols that reference *symbol_name* in their ``references`` list.

        A reference entry has the form ``"path/to/file.py:symbol_name"``.
        The match is performed against the suffix after the last ``:``.
        """
        results: list[ScipSymbol] = []
        for sym in self.symbols:
            for ref in sym.references:
                ref_sym_name = ref.rsplit(":", 1)[-1] if ":" in ref else ref
                if ref_sym_name == symbol_name:
                    results.append(sym)
                    break
        return results

    def symbols_in_file(self, relative_path: str) -> list[ScipSymbol]:
        """Return all symbols whose ``file_path`` equals *relative_path*."""
        return [s for s in self.symbols if s.file_path == relative_path]

    def cross_repo_deps(self, other: ScipIndex) -> list[tuple[ScipSymbol, ScipSymbol]]:
        """Return pairs ``(local_symbol, remote_symbol)`` where ``local_symbol``
        references a symbol that exists in ``other``.

        Matching is performed by symbol name: if a reference entry in a local
        symbol resolves to a name that appears in ``other.symbols``, each such
        remote symbol is paired with the local symbol.
        """
        other_by_name: dict[str, list[ScipSymbol]] = {}
        for sym in other.symbols:
            other_by_name.setdefault(sym.name, []).append(sym)

        pairs: list[tuple[ScipSymbol, ScipSymbol]] = []
        for local_sym in self.symbols:
            for ref in local_sym.references:
                ref_name = ref.rsplit(":", 1)[-1] if ":" in ref else ref
                for remote_sym in other_by_name.get(ref_name, []):
                    pairs.append((local_sym, remote_sym))
        return pairs


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def _parse_sidecar(data: object, repo_root: str) -> ScipIndex:
    """Parse a decoded JSON object into a :class:`ScipIndex`.

    Tolerates missing or malformed fields by using safe defaults.
    """
    if not isinstance(data, dict):
        return ScipIndex(repo_root=repo_root)

    symbols: list[ScipSymbol] = []
    documents_raw = data.get("documents", [])
    if not isinstance(documents_raw, list):
        return ScipIndex(repo_root=repo_root, symbols=symbols)

    for doc in documents_raw:
        if not isinstance(doc, dict):
            continue
        relative_path = str(doc.get("relative_path", ""))
        symbols_raw = doc.get("symbols", [])
        if not isinstance(symbols_raw, list):
            continue
        for sym_raw in symbols_raw:
            if not isinstance(sym_raw, dict):
                continue
            name = str(sym_raw.get("name", ""))
            if not name:
                continue
            kind = str(sym_raw.get("kind", ""))
            start_line_raw = sym_raw.get("start_line", 0)
            end_line_raw = sym_raw.get("end_line", 0)
            start_line = int(start_line_raw) if isinstance(start_line_raw, (int, float)) else 0
            end_line = int(end_line_raw) if isinstance(end_line_raw, (int, float)) else 0
            refs_raw = sym_raw.get("references", [])
            references: list[str] = (
                [str(r) for r in refs_raw if isinstance(r, str)]
                if isinstance(refs_raw, list)
                else []
            )
            symbols.append(
                ScipSymbol(
                    name=name,
                    kind=kind,
                    file_path=relative_path,
                    start_line=start_line,
                    end_line=end_line,
                    references=references,
                )
            )

    return ScipIndex(repo_root=repo_root, symbols=symbols)


def load_scip_index(repo_root: str) -> ScipIndex:
    """Load a SCIP index from ``<repo_root>/scip_index.json``.

    Returns an empty :class:`ScipIndex` if the sidecar file is absent or
    cannot be parsed.

    Args:
        repo_root: Absolute or relative path to the repository root directory.

    Returns:
        A populated :class:`ScipIndex` on success, or an empty one on failure.
    """
    sidecar_path = os.path.join(repo_root, _SIDECAR_FILENAME)
    try:
        with open(sidecar_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return ScipIndex(repo_root=repo_root)

    return _parse_sidecar(data, repo_root)
