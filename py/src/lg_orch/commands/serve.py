# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Christian Meurer — https://github.com/christianmeurer/Lula
"""serve_command — HTTP API server startup.

Thin wrapper that delegates to :func:`lg_orch.remote_api.serve_remote_api`.
Extracted from ``lg_orch.main.cli`` to keep the dispatcher under 200 lines.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from lg_orch.logging import get_logger
from lg_orch.long_term_memory import probe_ollama


def _log_embedding_provider() -> None:
    """Log the configured embedding provider status at startup."""
    log = get_logger()
    embed_provider = os.environ.get("LG_EMBED_PROVIDER", "stub")
    if embed_provider == "ollama":
        ollama_url = os.environ.get("LG_EMBED_OLLAMA_URL", "http://localhost:11434")
        if probe_ollama(ollama_url):
            log.info("embedding_provider_ready", provider="ollama", url=ollama_url)
        else:
            log.warning(
                "embedding_provider_unavailable",
                provider="ollama",
                url=ollama_url,
                fallback="stub_embedder",
            )
    else:
        log.info(
            "embedding_provider",
            provider="stub",
            note="set LG_EMBED_PROVIDER=ollama for semantic search",
        )


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

    _log_embedding_provider()

    from lg_orch.remote_api import serve_remote_api

    return serve_remote_api(repo_root=repo_root, host=str(args.host), port=port)
