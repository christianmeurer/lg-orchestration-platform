"""SPA file dispatcher used by the ThreadingHTTPServer in ``remote_api.py``.

:func:`create_spa_router` returns a callable that maps a URL subpath to a
``(status, content_type, body)`` triple — the same shape expected by
``_api_http_response()`` in ``remote_api.py``.

Routing rules
~~~~~~~~~~~~~
* ``style.css``  → serve ``spa/style.css``
* ``main.js``    → serve ``spa/main.js``
* anything else  → serve ``spa/index.html`` (SPA catch-all)
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

_MIME: dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
}

# Subpaths that are served as literal static files (not caught by the SPA router)
_STATIC_ASSETS = {"style.css", "main.js"}


def create_spa_router(
    spa_dir: Path,
) -> Callable[[str], tuple[int, str, bytes]]:
    """Return a dispatcher ``(subpath) -> (status, content_type, body)``.

    Parameters
    ----------
    spa_dir:
        Absolute path to the ``spa/`` package directory containing
        ``index.html``, ``style.css``, and ``main.js``.
    """

    def dispatch(subpath: str) -> tuple[int, str, bytes]:
        # Strip leading slashes so callers can pass raw path remainders
        clean = subpath.lstrip("/")

        if clean in _STATIC_ASSETS:
            asset_path = spa_dir / clean
            if not asset_path.is_file():
                return 404, "text/plain; charset=utf-8", b"asset not found"
            suffix = asset_path.suffix
            mime = _MIME.get(suffix, "application/octet-stream")
            return 200, mime, asset_path.read_bytes()

        # SPA catch-all: everything else routes to index.html
        index_path = spa_dir / "index.html"
        if not index_path.is_file():
            return 503, "text/plain; charset=utf-8", b"SPA index.html not found"
        return 200, "text/html; charset=utf-8", index_path.read_bytes()

    return dispatch
