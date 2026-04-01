# SPDX-License-Identifier: MIT
"""SPA static-file router for Leptos WASM build output."""

from __future__ import annotations

import mimetypes
import os
from collections.abc import Callable
from pathlib import Path

mimetypes.add_type("application/wasm", ".wasm")

_DEFAULT_DIST = (
    Path(__file__).resolve().parent.parent.parent.parent.parent / "rs" / "spa-leptos" / "dist"
)


def _dist_dir() -> Path:
    env = os.environ.get("LG_SPA_DIST_DIR")
    if env:
        return Path(env)
    return _DEFAULT_DIST


def create_spa_router() -> Callable[[str], tuple[int, str, bytes]]:
    """Return a dispatcher that serves Leptos dist/ files with SPA fallback."""

    def dispatch(subpath: str) -> tuple[int, str, bytes]:
        dist = _dist_dir()
        if not dist.is_dir():
            body = b"SPA dist not found. Run: cd rs/spa-leptos && trunk build"
            return 503, "text/plain; charset=utf-8", body

        target = dist / subpath if subpath else None
        if target and target.is_file() and dist in target.resolve().parents:
            mime = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            return 200, mime, target.read_bytes()

        index = dist / "index.html"
        if index.is_file():
            return 200, "text/html; charset=utf-8", index.read_bytes()

        return 404, "text/plain; charset=utf-8", b"index.html not found in dist"

    return dispatch
