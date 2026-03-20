# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Christian Meurer — https://github.com/christianmeurer/Lula
"""serve_command — HTTP API server startup.

Thin wrapper that delegates to :func:`lg_orch.remote_api.serve_remote_api`.
Extracted from ``lg_orch.main.cli`` to keep the dispatcher under 200 lines.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from lg_orch.logging import get_logger


def serve_command(args: Any, *, repo_root: Path) -> int:
    """Start the Remote API HTTP server.

    Parameters
    ----------
    args:
        Parsed argparse namespace from the ``serve-api`` subcommand.
        Expected attributes: ``host`` (str), ``port`` (int).
    repo_root:
        Resolved repository root path.
    """
    log = get_logger()
    port = int(args.port)
    if port <= 0 or port > 65535:
        log.error("remote_api_port_invalid", port=port)
        return 2

    from lg_orch.remote_api import serve_remote_api

    return serve_remote_api(repo_root=repo_root, host=str(args.host), port=port)
