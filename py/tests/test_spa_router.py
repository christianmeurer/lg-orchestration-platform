"""Tests for lg_orch.spa.router — SPA static-file routing."""

from __future__ import annotations

from pathlib import Path


def test_spa_router_returns_503_when_dist_missing(tmp_path: Path, monkeypatch: object) -> None:
    import os

    os.environ["LG_SPA_DIST_DIR"] = str(tmp_path / "nonexistent")
    try:
        from lg_orch.spa.router import create_spa_router

        dispatch = create_spa_router()
        status, _content_type, body = dispatch("")
        assert status == 503
        assert b"SPA dist not found" in body
    finally:
        os.environ.pop("LG_SPA_DIST_DIR", None)


def test_spa_router_serves_file_from_dist(tmp_path: Path) -> None:
    import os

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "style.css").write_text("body { color: red; }", encoding="utf-8")
    (dist / "index.html").write_text("<html>SPA</html>", encoding="utf-8")

    os.environ["LG_SPA_DIST_DIR"] = str(dist)
    try:
        from lg_orch.spa.router import create_spa_router

        dispatch = create_spa_router()

        status, content_type, body = dispatch("style.css")
        assert status == 200
        assert "text/css" in content_type
        assert b"color: red" in body
    finally:
        os.environ.pop("LG_SPA_DIST_DIR", None)


def test_spa_router_falls_back_to_index_html(tmp_path: Path) -> None:
    import os

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html>SPA Fallback</html>", encoding="utf-8")

    os.environ["LG_SPA_DIST_DIR"] = str(dist)
    try:
        from lg_orch.spa.router import create_spa_router

        dispatch = create_spa_router()
        status, content_type, body = dispatch("nonexistent/path")
        assert status == 200
        assert "text/html" in content_type
        assert b"SPA Fallback" in body
    finally:
        os.environ.pop("LG_SPA_DIST_DIR", None)


def test_spa_router_returns_404_when_no_index(tmp_path: Path) -> None:
    import os

    dist = tmp_path / "dist"
    dist.mkdir()
    # No index.html, no matching file

    os.environ["LG_SPA_DIST_DIR"] = str(dist)
    try:
        from lg_orch.spa.router import create_spa_router

        dispatch = create_spa_router()
        status, _content_type, body = dispatch("something")
        assert status == 404
        assert b"index.html not found" in body
    finally:
        os.environ.pop("LG_SPA_DIST_DIR", None)


def test_dist_dir_uses_env_var(tmp_path: Path) -> None:
    import os

    os.environ["LG_SPA_DIST_DIR"] = str(tmp_path)
    try:
        from lg_orch.spa.router import _dist_dir

        assert _dist_dir() == tmp_path
    finally:
        os.environ.pop("LG_SPA_DIST_DIR", None)
