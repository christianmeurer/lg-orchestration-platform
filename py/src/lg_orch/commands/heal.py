# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Christian Meurer — https://github.com/christianmeurer/Lula
"""heal_command — healing loop daemon mode.

Extracted from ``lg_orch.main.cli`` so the dispatcher stays under 200 lines.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from lg_orch.logging import get_logger


def heal_command(args: Any, *, repo_root: Path) -> int:
    """Run the healing loop as a foreground daemon.

    Parameters
    ----------
    args:
        Parsed argparse namespace from the ``heal`` subcommand.
        Expected attributes: ``repo_path`` (str, optional — defaults to
        *repo_root*), ``poll_interval`` (float, optional).
    repo_root:
        Resolved repository root path, used as default ``repo_path``.
    """
    log = get_logger()
    repo_path_raw = getattr(args, "repo_path", None)
    repo_path = str(repo_path_raw).strip() if repo_path_raw else str(repo_root)

    poll_interval_raw = getattr(args, "poll_interval", None)
    poll_interval: float
    try:
        poll_interval = float(poll_interval_raw) if poll_interval_raw is not None else 60.0
    except (TypeError, ValueError):
        poll_interval = 60.0

    if poll_interval < 1.0:
        log.error("heal_poll_interval_invalid", poll_interval=poll_interval)
        return 2

    from lg_orch.healing_loop import HealingLoop

    healing = HealingLoop(repo_path=repo_path, poll_interval_seconds=poll_interval)
    log.info("heal_daemon_starting", repo_path=repo_path, poll_interval=poll_interval)

    try:
        asyncio.run(healing.run_until_cancelled())
    except KeyboardInterrupt:
        pass

    return 0
