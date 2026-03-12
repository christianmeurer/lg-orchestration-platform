from __future__ import annotations

from dataclasses import dataclass
from html import escape
import json
from typing import Any


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


def _box(title: str, body_lines: list[str], *, width: int = 88) -> str:
    inner = max(40, width - 4)
    line_top = f"┌{'─' * inner}┐"
    line_bottom = f"└{'─' * inner}┘"
    title_line = f"│ {title[: inner - 2].ljust(inner - 2)} │"
    padded = [f"│ {ln[: inner - 2].ljust(inner - 2)} │" for ln in body_lines]
    return "\n".join([line_top, title_line, *padded, line_bottom])


def _duration_s(ts_ms: int, start_ms: int) -> float:
    return max(0.0, (ts_ms - start_ms) / 1000.0)


def _event_label(event: dict[str, Any]) -> str:
    kind = str(event.get("kind", "event"))
    data_raw = event.get("data", {})
    data = data_raw if isinstance(data_raw, dict) else {}
    name = str(data.get("name", "")).strip()
    phase = str(data.get("phase", "")).strip()
    label = " / ".join(part for part in (name, phase) if part)
    if label:
        return f"{kind}: {label}"
    if data:
        return f"{kind}: {json.dumps(data, ensure_ascii=False, sort_keys=True)[:120]}"
    return kind


def _tool_label(result: dict[str, Any]) -> str:
    tool = str(result.get("tool", "unknown"))
    exit_code = result.get("exit_code")
    if isinstance(exit_code, int):
        return f"{tool} (exit={exit_code})"
    return tool


def _html_card(title: str, body: str) -> str:
    return (
        '<section class="card">\n'
        f"  <h2>{escape(title)}</h2>\n"
        f"{body}\n"
        "</section>"
    )


def _html_document(*, title: str, body: str, include_mermaid: bool) -> str:
    mermaid_script = ""
    if include_mermaid:
        mermaid_script = (
            '<script type="module">\n'
            'import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs";\n'
            'mermaid.initialize({ startOnLoad: true, theme: "neutral" });\n'
            "</script>"
        )

    return "".join(
        [
            "<!DOCTYPE html>\n",
            '<html lang="en">\n',
            "<head>\n",
            '  <meta charset="utf-8">\n',
            '  <meta name="viewport" content="width=device-width, initial-scale=1">\n',
            f"  <title>{escape(title)}</title>\n",
            "  <style>\n",
            "    :root { color-scheme: dark; }\n",
            "    body { margin: 0; background: #0f172a; color: #e2e8f0; font-family: Segoe UI, Arial, sans-serif; }\n",
            "    main { max-width: 1120px; margin: 0 auto; padding: 24px; display: grid; gap: 16px; }\n",
            "    .card { background: #111827; border: 1px solid #334155; border-radius: 12px; padding: 16px 18px; }\n",
            "    h1, h2 { margin: 0 0 12px; }\n",
            "    h1 { font-size: 1.35rem; }\n",
            "    h2 { font-size: 1.05rem; }\n",
            "    .summary, .items { list-style: none; padding: 0; margin: 0; display: grid; gap: 10px; }\n",
            "    .summary li { display: grid; gap: 4px; }\n",
            "    .items li { display: flex; gap: 12px; align-items: flex-start; padding-top: 8px; border-top: 1px solid #1f2937; }\n",
            "    .items li:first-child { border-top: 0; padding-top: 0; }\n",
            "    .stack { display: grid; gap: 4px; }\n",
            "    .muted { color: #94a3b8; }\n",
            "    .links { margin-left: auto; white-space: nowrap; }\n",
            "    .mono, pre { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; }\n",
            "    .mono { min-width: 84px; color: #93c5fd; }\n",
            "    .badge { min-width: 44px; padding: 2px 8px; border-radius: 999px; font-size: 0.75rem; text-align: center; }\n",
            "    .badge.ok { background: #14532d; color: #bbf7d0; }\n",
            "    .badge.err { background: #7f1d1d; color: #fecaca; }\n",
            "    a { color: #93c5fd; text-decoration: none; }\n",
            "    a:hover { text-decoration: underline; }\n",
            "    pre { margin: 0; white-space: pre-wrap; overflow-x: auto; }\n",
            "  </style>\n",
            "</head>\n",
            "<body>\n",
            "<main>\n",
            body,
            "\n</main>\n",
            mermaid_script,
            "\n</body>\n",
            "</html>\n",
        ]
    )


def render_timeline(events: list[dict[str, Any]], *, width: int = 88, max_rows: int = 14) -> str:
    if not events:
        return _box("Timeline", ["No events captured."])

    start_ms = int(events[0].get("ts_ms", 0))
    visible = events[-max_rows:]
    lines: list[str] = []
    for idx, ev in enumerate(visible, start=1):
        ts_ms = int(ev.get("ts_ms", start_ms))
        kind = str(ev.get("kind", "event"))
        delta = _duration_s(ts_ms, start_ms)
        bar_units = min(24, int(delta * 3))
        bar = "█" * bar_units if bar_units > 0 else "·"
        lines.append(f"{idx:02d}  +{delta:6.2f}s  {kind:<22} {bar}")

    return _box("Timeline", lines, width=width)


def render_tool_results(
    tool_results: list[dict[str, Any]], *, width: int = 88, max_rows: int = 8
) -> str:
    if not tool_results:
        return _box("Tool Results", ["No tool invocations captured."])

    visible = tool_results[-max_rows:]
    lines: list[str] = []
    for idx, result in enumerate(visible, start=1):
        tool = str(result.get("tool", "unknown"))
        ok = bool(result.get("ok", False))
        marker = "OK" if ok else "ERR"
        lines.append(f"{idx:02d}  [{marker}] {tool}")
    return _box("Tool Results", lines, width=width)


def render_run_header(*, request: str, intent: str | None) -> str:
    req = request.strip() or "(empty request)"
    intent_line = f"intent: {intent}" if intent else "intent: (pending)"
    return _box("Lula Console", [f"request: {req}", intent_line])


def render_trace_dashboard(payload: dict[str, Any], *, width: int = 88) -> str:
    request = str(payload.get("request", ""))
    intent_raw = payload.get("intent")
    intent = str(intent_raw) if isinstance(intent_raw, str) else None
    events_raw = payload.get("events", [])
    tools_raw = payload.get("tool_results", [])
    final = str(payload.get("final", ""))

    events = [e for e in events_raw if isinstance(e, dict)]
    tool_results = [t for t in tools_raw if isinstance(t, dict)]
    final_lines = final.splitlines() or ["(empty)"]

    sections = [
        render_run_header(request=request, intent=intent),
        render_timeline(events, width=width),
        render_tool_results(tool_results, width=width),
        _box("Final Output", final_lines[:8], width=width),
    ]
    return "\n\n".join(sections) + "\n"


def render_trace_dashboard_html(
    payload: dict[str, Any],
    *,
    mermaid_graph: str | None = None,
    index_href: str | None = None,
    trace_json_href: str | None = None,
) -> str:
    request = str(payload.get("request", "")).strip() or "(empty request)"
    intent_raw = payload.get("intent")
    intent = str(intent_raw) if isinstance(intent_raw, str) and intent_raw.strip() else "(pending)"
    run_id_raw = payload.get("run_id")
    run_id = str(run_id_raw).strip() if isinstance(run_id_raw, str) else "(not captured)"
    verification_raw = payload.get("verification", {})
    verification = verification_raw if isinstance(verification_raw, dict) else {}
    checkpoint_raw = payload.get("checkpoint", {})
    checkpoint = checkpoint_raw if isinstance(checkpoint_raw, dict) else {}
    events_raw = payload.get("events", [])
    tools_raw = payload.get("tool_results", [])
    final = str(payload.get("final", ""))

    events = [e for e in events_raw if isinstance(e, dict)]
    tool_results = [t for t in tools_raw if isinstance(t, dict)]

    if events:
        start_ms = int(events[0].get("ts_ms", 0))
        timeline_items = "\n".join(
            (
                "<li>"
                f'<span class="mono">+{_duration_s(int(event.get("ts_ms", start_ms)), start_ms):0.2f}s</span>'
                f"<span>{escape(_event_label(event))}</span>"
                "</li>"
            )
            for event in events[-24:]
        )
    else:
        timeline_items = '<li><span>No events captured.</span></li>'

    if tool_results:
        tool_items = "\n".join(
            (
                "<li>"
                f'<span class="badge {'ok' if bool(result.get("ok", False)) else 'err'}">'
                f"{'OK' if bool(result.get('ok', False)) else 'ERR'}</span>"
                f"<span>{escape(_tool_label(result))}</span>"
                "</li>"
            )
            for result in tool_results[-24:]
        )
    else:
        tool_items = '<li><span>No tool invocations captured.</span></li>'

    graph_body = (
        '<pre class="mermaid">'
        f"{escape(mermaid_graph.strip())}"
        "</pre>"
        if mermaid_graph and mermaid_graph.strip()
        else "<p>No graph available.</p>"
    )

    summary_lines = [
        f"  <li><strong>request</strong><span>{escape(request)}</span></li>",
        f"  <li><strong>intent</strong><span>{escape(intent)}</span></li>",
        f"  <li><strong>run_id</strong><span>{escape(run_id)}</span></li>",
    ]
    if "ok" in verification:
        verification_text = "passed" if bool(verification.get("ok", False)) else "failed"
        summary_lines.append(
            f"  <li><strong>verification</strong><span>{escape(verification_text)}</span></li>"
        )
    checkpoint_thread_id = str(checkpoint.get("thread_id", "")).strip()
    checkpoint_id = str(
        checkpoint.get("latest_checkpoint_id") or checkpoint.get("resume_checkpoint_id") or ""
    ).strip()
    if checkpoint_thread_id or checkpoint_id:
        checkpoint_text = " · ".join(
            part
            for part in [
                f"thread={checkpoint_thread_id}" if checkpoint_thread_id else "",
                f"checkpoint={checkpoint_id}" if checkpoint_id else "",
            ]
            if part
        )
        summary_lines.append(
            f"  <li><strong>checkpoint</strong><span>{escape(checkpoint_text)}</span></li>"
        )
    if index_href:
        summary_lines.append(
            "  <li><strong>site</strong>"
            f'<span><a href="{escape(index_href)}">All runs</a></span></li>'
        )
    if trace_json_href:
        summary_lines.append(
            "  <li><strong>trace</strong>"
            f'<span><a href="{escape(trace_json_href)}">Raw JSON</a></span></li>'
        )

    cards = [
        _html_card(
            "Lula Dashboard",
            '<ul class="summary">\n' + "\n".join(summary_lines) + "\n</ul>",
        ),
        _html_card("Graph", graph_body),
        _html_card("Timeline", f'<ul class="items">\n{timeline_items}\n</ul>'),
        _html_card("Tool Results", f'<ul class="items">\n{tool_items}\n</ul>'),
        _html_card("Final Output", f'<pre>{escape(final or "(empty)")}</pre>'),
    ]

    return _html_document(
        title="Lula Dashboard",
        body="\n".join(cards),
        include_mermaid=bool(mermaid_graph and mermaid_graph.strip()),
    )


def render_trace_site_index_html(runs: list[dict[str, Any]]) -> str:
    if runs:
        def _run_metadata_suffix(run: dict[str, Any]) -> str:
            parts: list[str] = []
            verification_ok = run.get("verification_ok")
            if verification_ok is True:
                parts.append("verify=ok")
            elif verification_ok is False:
                parts.append("verify=fail")
            checkpoint_id = str(run.get("checkpoint_id", "")).strip()
            if checkpoint_id:
                parts.append(f"checkpoint={checkpoint_id}")
            if not parts:
                return ""
            return " · " + " · ".join(parts)

        items = "\n".join(
            (
                "<li>"
                '<div class="stack">'
                f'<a href="{escape(str(run.get("dashboard_href", "index.html")))}">'
                f'{escape(str(run.get("request", "(empty request)")))}</a>'
                 f'<span class="muted">run_id={escape(str(run.get("run_id", "")))} '
                 f'· intent={escape(str(run.get("intent", "(pending)")))} '
                 f'· events={int(run.get("events_count", 0) or 0)} '
                 f'· tools={int(run.get("tool_results_count", 0) or 0)}'
                 f'{escape(_run_metadata_suffix(run))}</span>'
                 "</div>"
                 '<span class="links">'
                 f'<a href="{escape(str(run.get("dashboard_href", "index.html")))}">Dashboard</a>'
                 " · "
                 f'<a href="{escape(str(run.get("trace_href", "#")))}">Trace JSON</a>'
                "</span>"
                "</li>"
            )
            for run in runs
        )
    else:
        items = '<li><span>No runs captured yet.</span></li>'

    cards = [
        _html_card(
            "Lula Trace Site",
            (
                "<p>Static dashboards generated from file-based run traces.</p>\n"
                "<p class=\"muted\">Open a dashboard for graph, timeline, tool results, and final output.</p>"
            ),
        ),
        _html_card("Runs", f'<ul class="items">\n{items}\n</ul>'),
    ]
    return _html_document(
        title="Lula Trace Site",
        body="\n".join(cards),
        include_mermaid=False,
    )
