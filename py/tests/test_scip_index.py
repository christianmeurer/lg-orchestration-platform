"""Tests for py/src/lg_orch/scip_index.py (Wave 9)."""

from __future__ import annotations

import json
import os
import tempfile

from lg_orch.scip_index import ScipIndex, load_scip_index

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_sidecar(directory: str, data: object) -> None:
    path = os.path.join(directory, "scip_index.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)


def _minimal_sidecar(
    relative_path: str = "src/foo.py",
    symbols: list[dict] | None = None,
) -> dict:
    return {
        "documents": [
            {
                "relative_path": relative_path,
                "symbols": symbols or [],
            }
        ]
    }


# ---------------------------------------------------------------------------
# 1. Empty index for missing file
# ---------------------------------------------------------------------------


def test_load_scip_index_returns_empty_for_missing_file() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        index = load_scip_index(tmpdir)

    assert isinstance(index, ScipIndex)
    assert index.symbols == []
    assert index.repo_root == tmpdir


# ---------------------------------------------------------------------------
# 2. Parsing a valid sidecar JSON
# ---------------------------------------------------------------------------


def test_load_scip_index_parses_sidecar_json() -> None:
    data = _minimal_sidecar(
        relative_path="auth/service.py",
        symbols=[
            {
                "name": "AuthService",
                "kind": "class",
                "start_line": 5,
                "end_line": 80,
                "references": ["main.py:build_app"],
            },
            {
                "name": "login",
                "kind": "function",
                "start_line": 10,
                "end_line": 25,
                "references": [],
            },
        ],
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        _write_sidecar(tmpdir, data)
        index = load_scip_index(tmpdir)

    assert len(index.symbols) == 2
    names = {s.name for s in index.symbols}
    assert names == {"AuthService", "login"}

    auth_sym = next(s for s in index.symbols if s.name == "AuthService")
    assert auth_sym.kind == "class"
    assert auth_sym.file_path == "auth/service.py"
    assert auth_sym.start_line == 5
    assert auth_sym.end_line == 80
    assert auth_sym.references == ["main.py:build_app"]


# ---------------------------------------------------------------------------
# 3. find_symbol by name
# ---------------------------------------------------------------------------


def test_find_symbol_by_name() -> None:
    data = _minimal_sidecar(
        symbols=[
            {
                "name": "validate_token",
                "kind": "function",
                "start_line": 1,
                "end_line": 10,
                "references": [],
            },
            {
                "name": "TokenError",
                "kind": "class",
                "start_line": 15,
                "end_line": 20,
                "references": [],
            },
        ]
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        _write_sidecar(tmpdir, data)
        index = load_scip_index(tmpdir)

    results = index.find_symbol("validate_token")
    assert len(results) == 1
    assert results[0].name == "validate_token"
    assert results[0].kind == "function"

    missing = index.find_symbol("does_not_exist")
    assert missing == []


# ---------------------------------------------------------------------------
# 4. find_references
# ---------------------------------------------------------------------------


def test_find_references() -> None:
    data = {
        "documents": [
            {
                "relative_path": "a.py",
                "symbols": [
                    {
                        "name": "caller_a",
                        "kind": "function",
                        "start_line": 1,
                        "end_line": 5,
                        "references": ["b.py:target_fn"],
                    }
                ],
            },
            {
                "relative_path": "c.py",
                "symbols": [
                    {
                        "name": "caller_c",
                        "kind": "function",
                        "start_line": 1,
                        "end_line": 3,
                        "references": ["b.py:other_fn"],
                    }
                ],
            },
        ]
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        _write_sidecar(tmpdir, data)
        index = load_scip_index(tmpdir)

    refs = index.find_references("target_fn")
    assert len(refs) == 1
    assert refs[0].name == "caller_a"

    no_refs = index.find_references("unknown_fn")
    assert no_refs == []


# ---------------------------------------------------------------------------
# 5. symbols_in_file
# ---------------------------------------------------------------------------


def test_symbols_in_file() -> None:
    data = {
        "documents": [
            {
                "relative_path": "api/views.py",
                "symbols": [
                    {
                        "name": "index",
                        "kind": "function",
                        "start_line": 1,
                        "end_line": 5,
                        "references": [],
                    },
                    {
                        "name": "detail",
                        "kind": "function",
                        "start_line": 7,
                        "end_line": 12,
                        "references": [],
                    },
                ],
            },
            {
                "relative_path": "api/models.py",
                "symbols": [
                    {
                        "name": "User",
                        "kind": "class",
                        "start_line": 1,
                        "end_line": 30,
                        "references": [],
                    },
                ],
            },
        ]
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        _write_sidecar(tmpdir, data)
        index = load_scip_index(tmpdir)

    views_syms = index.symbols_in_file("api/views.py")
    assert len(views_syms) == 2
    assert {s.name for s in views_syms} == {"index", "detail"}

    model_syms = index.symbols_in_file("api/models.py")
    assert len(model_syms) == 1
    assert model_syms[0].name == "User"

    empty = index.symbols_in_file("nonexistent.py")
    assert empty == []


# ---------------------------------------------------------------------------
# 6. cross_repo_deps finds matching references
# ---------------------------------------------------------------------------


def test_cross_repo_deps_finds_matching_references() -> None:
    local_data = _minimal_sidecar(
        relative_path="gateway.py",
        symbols=[
            {
                "name": "call_auth",
                "kind": "function",
                "start_line": 1,
                "end_line": 10,
                "references": ["auth_service.py:AuthService"],
            }
        ],
    )
    remote_data = _minimal_sidecar(
        relative_path="auth_service.py",
        symbols=[
            {
                "name": "AuthService",
                "kind": "class",
                "start_line": 1,
                "end_line": 50,
                "references": [],
            }
        ],
    )

    with tempfile.TemporaryDirectory() as local_dir, tempfile.TemporaryDirectory() as remote_dir:
        _write_sidecar(local_dir, local_data)
        _write_sidecar(remote_dir, remote_data)

        local_index = load_scip_index(local_dir)
        remote_index = load_scip_index(remote_dir)

    pairs = local_index.cross_repo_deps(remote_index)

    assert len(pairs) == 1
    local_sym, remote_sym = pairs[0]
    assert local_sym.name == "call_auth"
    assert remote_sym.name == "AuthService"


def test_cross_repo_deps_returns_empty_when_no_overlap() -> None:
    local_data = _minimal_sidecar(
        symbols=[
            {
                "name": "no_match",
                "kind": "function",
                "start_line": 1,
                "end_line": 5,
                "references": ["x.py:something_else"],
            }
        ]
    )
    remote_data = _minimal_sidecar(
        symbols=[
            {
                "name": "unrelated",
                "kind": "class",
                "start_line": 1,
                "end_line": 20,
                "references": [],
            }
        ]
    )

    with tempfile.TemporaryDirectory() as local_dir, tempfile.TemporaryDirectory() as remote_dir:
        _write_sidecar(local_dir, local_data)
        _write_sidecar(remote_dir, remote_data)

        local_index = load_scip_index(local_dir)
        remote_index = load_scip_index(remote_dir)

    pairs = local_index.cross_repo_deps(remote_index)
    assert pairs == []


# ---------------------------------------------------------------------------
# FIX 11.3: ScipIndex stale tracking
# ---------------------------------------------------------------------------


def test_scip_index_is_not_stale_by_default() -> None:
    from lg_orch.scip_index import ScipIndex
    idx = ScipIndex(repo_root="/tmp/test", symbols=[])
    assert not idx.is_stale


def test_scip_index_mark_stale() -> None:
    from lg_orch.scip_index import ScipIndex
    idx = ScipIndex(repo_root="/tmp/test", symbols=[])
    idx.mark_stale()
    assert idx.is_stale
