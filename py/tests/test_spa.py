"""Tests for the standalone SPA served under /app/*.

Covers:
* GET /app         → index.html (text/html)
* GET /app/style.css → style.css (text/css)
* GET /app/main.js  → main.js (application/javascript)
"""
from __future__ import annotations

from pathlib import Path

import pytest

from lg_orch.remote_api import RemoteAPIService, _api_http_response

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class DummyProcess:
    """Minimal subprocess.Popen stub used to avoid spawning real processes."""

    def __init__(self) -> None:
        import io

        self.stdout = io.StringIO("")
        self._returncode = 0

    def poll(self) -> int | None:
        return self._returncode

    def wait(self) -> int:
        return self._returncode

    def terminate(self) -> None:
        pass


# ---------------------------------------------------------------------------
# SPA static-file tests
# ---------------------------------------------------------------------------


def test_spa_index_returns_html(tmp_path: Path) -> None:
    """GET /app returns the SPA index.html with Content-Type text/html."""
    service = RemoteAPIService(repo_root=tmp_path)

    # The spa/ directory is discovered relative to remote_api.py at runtime.
    # It must already exist (it ships with the package).
    spa_dir = Path(__file__).parent.parent / "src" / "lg_orch" / "spa"
    if not spa_dir.exists():
        pytest.skip("spa/ directory not present — skipping file-serving test")

    status, content_type, body = _api_http_response(
        service,
        method="GET",
        request_path="/app",
        request_body=None,
    )

    assert status == 200, f"expected 200, got {status}: {body[:200]}"
    assert "text/html" in content_type
    # The SPA must be self-contained — no external script tags
    html = body.decode("utf-8")
    assert "<!DOCTYPE html>" in html or "<!doctype html>" in html.lower()
    # Inline styles and scripts present
    assert "<style>" in html
    assert "<script>" in html
    # Must reference EventSource for SSE connectivity
    assert "EventSource" in html


def test_spa_style_css_returns_css(tmp_path: Path) -> None:
    """GET /app/style.css returns the stylesheet with Content-Type text/css."""
    service = RemoteAPIService(repo_root=tmp_path)

    spa_dir = Path(__file__).parent.parent / "src" / "lg_orch" / "spa"
    if not (spa_dir / "style.css").exists():
        pytest.skip("spa/style.css not present — skipping test")

    status, content_type, body = _api_http_response(
        service,
        method="GET",
        request_path="/app/style.css",
        request_body=None,
    )

    assert status == 200, f"expected 200, got {status}"
    assert "text/css" in content_type
    css = body.decode("utf-8")
    # Verify GitHub dark palette token and key structural rules are present
    assert "#0d1117" in css          # background token
    assert "#c9d1d9" in css          # text colour token
    assert "node-pulse" in css       # pipeline animation keyframe
    assert "event-row" in css        # event log row selector


def test_spa_main_js_returns_js(tmp_path: Path) -> None:
    """GET /app/main.js returns the JS with Content-Type application/javascript."""
    service = RemoteAPIService(repo_root=tmp_path)

    spa_dir = Path(__file__).parent.parent / "src" / "lg_orch" / "spa"
    if not (spa_dir / "main.js").exists():
        pytest.skip("spa/main.js not present — skipping test")

    status, content_type, body = _api_http_response(
        service,
        method="GET",
        request_path="/app/main.js",
        request_body=None,
    )

    assert status == 200, f"expected 200, got {status}"
    assert "javascript" in content_type
    js = body.decode("utf-8")
    # Verify key identifiers are present
    assert "EventSource" in js           # SSE connectivity
    assert "PIPELINE_NODES" in js        # pipeline graph data
    assert "approveRun" in js            # approval action
    assert "rejectRun" in js             # rejection action
    assert "refreshRunList" in js        # sidebar polling


def test_index_html_contains_d3_cdn_script(tmp_path: Path) -> None:
    """index.html must include the D3 v7 CDN script tag (jsDelivr)."""
    spa_dir = Path(__file__).parent.parent / "src" / "lg_orch" / "spa"
    index_path = spa_dir / "index.html"
    if not index_path.exists():
        pytest.skip("spa/index.html not present — skipping test")

    html = index_path.read_text(encoding="utf-8")
    assert "cdn.jsdelivr.net/npm/d3@7" in html, (
        "index.html must load D3 v7 from the jsDelivr CDN"
    )


def test_main_js_uses_force_simulation(tmp_path: Path) -> None:
    """main.js must contain 'forceSimulation' confirming D3 graph is wired."""
    spa_dir = Path(__file__).parent.parent / "src" / "lg_orch" / "spa"
    main_js_path = spa_dir / "main.js"
    if not main_js_path.exists():
        pytest.skip("spa/main.js not present — skipping test")

    js = main_js_path.read_text(encoding="utf-8")
    assert "forceSimulation" in js, (
        "main.js must use d3.forceSimulation to build the agent activity graph"
    )
