# SPDX-License-Identifier: MIT
"""Rich console utilities for the Lula CLI."""

from __future__ import annotations

from rich.console import Console
from rich.theme import Theme

LULA_THEME = Theme({
    "lula.accent": "bold cyan",
    "lula.ok": "bold green",
    "lula.err": "bold red",
    "lula.warn": "bold yellow",
    "lula.info": "bold magenta",
    "lula.muted": "dim",
    "lula.node": "bold blue",
    "lula.tool": "dim cyan",
    "lula.header": "bold cyan on default",
})

# stdout for user-facing output
console = Console(theme=LULA_THEME, highlight=False)

# stderr for logs — keeps structured log output separate from rich UI
err_console = Console(theme=LULA_THEME, stderr=True, highlight=False)
