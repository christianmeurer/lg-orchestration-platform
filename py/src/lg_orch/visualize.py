from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GraphEdge:
    src: str
    dst: str


def graph_mermaid(*, nodes: list[str], edges: list[GraphEdge], direction: str = "LR") -> str:
    lines: list[str] = []
    lines.append(f"flowchart {direction}")
    for n in nodes:
        safe = n.replace('"', "")
        lines.append(f'  {safe}["{safe}"]')
    for e in edges:
        lines.append(f"  {e.src} --> {e.dst}")
    return "\n".join(lines) + "\n"
