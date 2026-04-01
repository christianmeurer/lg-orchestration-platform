# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Christian Meurer — https://github.com/christianmeurer/Lula
from __future__ import annotations

import json
from dataclasses import dataclass
from html import escape
from typing import Any

from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from lg_orch.console import console


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


def _panel(
    title: str, body_lines: list[str], *, width: int = 88, style: str = "lula.accent"
) -> Panel:
    body = Text("\n".join(body_lines))
    return Panel(
        body,
        title=title,
        title_align="left",
        width=min(width, console.width),
        border_style=style,
        padding=(0, 1),
    )


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
    return f'<section class="card">\n  <h2>{escape(title)}</h2>\n{body}\n</section>'


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
            "    body { margin: 0; background: #0f172a; color: #e2e8f0;"
            " font-family: Segoe UI, Arial, sans-serif; }\n",
            "    main { max-width: 1120px; margin: 0 auto; padding: 24px;"
            " display: grid; gap: 16px; }\n",
            "    .card { background: #111827; border: 1px solid #334155;"
            " border-radius: 12px; padding: 16px 18px; }\n",
            "    h1, h2 { margin: 0 0 12px; }\n",
            "    h1 { font-size: 1.35rem; }\n",
            "    h2 { font-size: 1.05rem; }\n",
            "    .summary, .items { list-style: none; padding: 0; margin: 0;"
            " display: grid; gap: 10px; }\n",
            "    .summary li { display: grid; gap: 4px; }\n",
            "    .items li { display: flex; gap: 12px; align-items: flex-start;"
            " padding-top: 8px; border-top: 1px solid #1f2937; }\n",
            "    .items li:first-child { border-top: 0; padding-top: 0; }\n",
            "    .stack { display: grid; gap: 4px; }\n",
            "    .muted { color: #94a3b8; }\n",
            "    .links { margin-left: auto; white-space: nowrap; }\n",
            "    .mono, pre { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; }\n",
            "    .mono { min-width: 84px; color: #93c5fd; }\n",
            "    .badge { min-width: 44px; padding: 2px 8px; border-radius: 999px;"
            " font-size: 0.75rem; text-align: center; }\n",
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


def render_timeline(events: list[dict[str, Any]], *, width: int = 88, max_rows: int = 14) -> None:
    if not events:
        console.print(_panel("Timeline", ["No events captured."], width=width, style="lula.muted"))
        return

    table = Table(
        title="Timeline",
        title_style="lula.accent",
        width=min(width, console.width),
        border_style="cyan",
        show_lines=False,
        pad_edge=True,
    )
    table.add_column("#", style="dim", width=4)
    table.add_column("Offset", style="lula.muted", width=10)
    table.add_column("Kind", style="lula.node", min_width=16)
    table.add_column("Bar", min_width=12)

    start_ms = int(events[0].get("ts_ms", 0))
    visible = events[-max_rows:]
    for idx, ev in enumerate(visible, start=1):
        ts_ms = int(ev.get("ts_ms", start_ms))
        kind = str(ev.get("kind", "event"))
        delta = _duration_s(ts_ms, start_ms)
        bar_units = min(24, int(delta * 3))
        bar_text = (
            Text("\u2588" * bar_units, style="green")
            if bar_units > 0
            else Text("\u00b7", style="dim")
        )
        table.add_row(f"{idx:02d}", f"+{delta:.2f}s", kind, bar_text)

    console.print(table)


def render_tool_results(
    tool_results: list[dict[str, Any]], *, width: int = 88, max_rows: int = 8
) -> None:
    if not tool_results:
        console.print(
            _panel(
                "Tool Results", ["No tool invocations captured."], width=width, style="lula.muted"
            )
        )
        return

    table = Table(
        title="Tool Results",
        title_style="lula.accent",
        width=min(width, console.width),
        border_style="cyan",
        show_lines=False,
        pad_edge=True,
    )
    table.add_column("#", style="dim", width=4)
    table.add_column("Status", width=6)
    table.add_column("Tool", style="lula.tool")

    visible = tool_results[-max_rows:]
    for idx, result in enumerate(visible, start=1):
        tool = str(result.get("tool", "unknown"))
        ok = bool(result.get("ok", False))
        status = Text("OK", style="lula.ok") if ok else Text("ERR", style="lula.err")
        table.add_row(f"{idx:02d}", status, tool)

    console.print(table)


def render_run_header(*, request: str, intent: str | None) -> None:
    req = request.strip() or "(empty request)"
    intent_line = f"intent: {intent}" if intent else "intent: (pending)"
    body = Text()
    body.append("request: ", style="lula.muted")
    body.append(req + "\n")
    body.append("intent:  ", style="lula.muted")
    body.append(intent_line.removeprefix("intent: "))
    console.print(
        Panel(
            body,
            title="Lula Console",
            title_align="left",
            border_style="lula.header",
            padding=(0, 1),
        )
    )


def render_trace_dashboard(payload: dict[str, Any], *, width: int = 88) -> None:
    request = str(payload.get("request", ""))
    intent_raw = payload.get("intent")
    intent = str(intent_raw) if isinstance(intent_raw, str) else None
    events_raw = payload.get("events", [])
    tools_raw = payload.get("tool_results", [])
    verification_raw = payload.get("verification", {})
    verification = verification_raw if isinstance(verification_raw, dict) else {}
    approval_raw = payload.get("approval", {})
    approval = approval_raw if isinstance(approval_raw, dict) else {}
    halt_reason = str(payload.get("halt_reason", "")).strip()
    final = str(payload.get("final", ""))

    events = [e for e in events_raw if isinstance(e, dict)]
    tool_results = [t for t in tools_raw if isinstance(t, dict)]
    final_lines = final.splitlines() or ["(empty)"]
    summary_lines: list[str] = []
    if "ok" in verification:
        v_ok = bool(verification.get("ok", False))
        summary_lines.append(f"verification: {'passed' if v_ok else 'failed'}")
    if "acceptance_ok" in verification:
        a_ok = bool(verification.get("acceptance_ok", False))
        summary_lines.append(f"acceptance: {'passed' if a_ok else 'failed'}")
    if halt_reason:
        summary_lines.append(f"halt_reason: {halt_reason}")
    if bool(approval.get("pending", False)):
        summary_lines.append("approval: pending")
    history_raw = approval.get("history", [])
    history = history_raw if isinstance(history_raw, list) else []
    if history:
        summary_lines.append(f"approval_history: {len(history)}")
    if not summary_lines:
        summary_lines.append("No verification summary captured.")

    render_run_header(request=request, intent=intent)
    console.print(_panel("Verification", summary_lines, width=width, style="lula.warn"))
    render_timeline(events, width=width)
    render_tool_results(tool_results, width=width)
    console.print(_panel("Final Output", final_lines[:8], width=width, style="lula.ok"))


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
    telemetry_raw = payload.get("telemetry", {})
    telemetry = telemetry_raw if isinstance(telemetry_raw, dict) else {}
    diagnostics_raw = telemetry.get("diagnostics", [])
    diagnostics = [entry for entry in diagnostics_raw if isinstance(entry, dict)]
    context_budget_raw = telemetry.get("context_budget", {})
    context_budget = context_budget_raw if isinstance(context_budget_raw, dict) else {}
    working_set_raw = context_budget.get("working_set", {})
    working_set = working_set_raw if isinstance(working_set_raw, dict) else {}
    checkpoint_raw = payload.get("checkpoint", {})
    checkpoint = checkpoint_raw if isinstance(checkpoint_raw, dict) else {}
    approval_raw = payload.get("approval", {})
    approval = approval_raw if isinstance(approval_raw, dict) else {}
    events_raw = payload.get("events", [])
    tools_raw = payload.get("tool_results", [])
    halt_reason = str(payload.get("halt_reason", "")).strip()
    final = str(payload.get("final", ""))

    events = [e for e in events_raw if isinstance(e, dict)]
    tool_results = [t for t in tools_raw if isinstance(t, dict)]

    if events:
        start_ms = int(events[0].get("ts_ms", 0))
        timeline_items = "\n".join(
            (
                "<li>"
                f'<span class="mono">'
                f"+{_duration_s(int(event.get('ts_ms', start_ms)), start_ms):0.2f}s</span>"
                f"<span>{escape(_event_label(event))}</span>"
                "</li>"
            )
            for event in events[-24:]
        )
    else:
        timeline_items = "<li><span>No events captured.</span></li>"

    if tool_results:
        tool_items = "\n".join(
            (
                "<li>"
                f'<span class="badge {"ok" if bool(result.get("ok", False)) else "err"}">'
                f"{'OK' if bool(result.get('ok', False)) else 'ERR'}</span>"
                f"<span>{escape(_tool_label(result))}</span>"
                "</li>"
            )
            for result in tool_results[-24:]
        )
    else:
        tool_items = "<li><span>No tool invocations captured.</span></li>"

    graph_body = (
        f'<pre class="mermaid">{escape(mermaid_graph.strip())}</pre>'
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
    if "acceptance_ok" in verification:
        acceptance_text = "passed" if bool(verification.get("acceptance_ok", False)) else "failed"
        summary_lines.append(
            f"  <li><strong>acceptance</strong><span>{escape(acceptance_text)}</span></li>"
        )
    if halt_reason:
        summary_lines.append(
            f"  <li><strong>halt_reason</strong><span>{escape(halt_reason)}</span></li>"
        )
    working_set_tokens = working_set.get("token_estimate", 0)
    if isinstance(working_set_tokens, int) and working_set_tokens > 0:
        summary_lines.append(
            f"  <li><strong>working_set_tokens</strong><span>{working_set_tokens}</span></li>"
        )
    if diagnostics:
        summary_lines.append(
            f"  <li><strong>diagnostics</strong><span>{len(diagnostics)}</span></li>"
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
    if bool(approval.get("pending", False)):
        summary_lines.append(
            "  <li><strong>approval</strong><span>"
            + escape(str(approval.get("summary", "pending approval")).strip() or "pending approval")
            + "</span></li>"
        )
    approval_history_raw = approval.get("history", [])
    approval_history = (
        [entry for entry in approval_history_raw if isinstance(entry, dict)]
        if isinstance(approval_history_raw, list)
        else []
    )
    if approval_history:
        summary_lines.append(
            f"  <li><strong>approval_history</strong><span>{len(approval_history)}</span></li>"
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
        _html_card("Final Output", f"<pre>{escape(final or '(empty)')}</pre>"),
    ]

    if approval_history:
        approval_items = "\n".join(
            (
                "<li>"
                f'<span class="mono">{escape(str(entry.get("ts", "")))}</span>'
                '<div class="stack">'
                f"<strong>{escape(str(entry.get('decision', '')).strip() or 'decision')}</strong>"
                f'<span class="muted">actor='
                f"{escape(str(entry.get('actor', '')).strip() or 'unknown')} "
                f"challenge={escape(str(entry.get('challenge_id', '')).strip() or '—')}</span>"
                f"<span>"
                f"{escape(str(entry.get('rationale', '')).strip() or '(no rationale)')}</span>"
                "</div>"
                "</li>"
            )
            for entry in approval_history[-12:]
        )
        cards.insert(
            1, _html_card("Approval History", f'<ul class="items">\n{approval_items}\n</ul>')
        )

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
            acceptance_ok = run.get("acceptance_ok")
            if acceptance_ok is True:
                parts.append("accept=ok")
            elif acceptance_ok is False:
                parts.append("accept=fail")
            halt_reason = str(run.get("halt_reason", "")).strip()
            if halt_reason:
                parts.append(f"halt={halt_reason}")
            working_set_tokens = run.get("working_set_tokens", 0)
            if isinstance(working_set_tokens, int) and working_set_tokens > 0:
                parts.append(f"working_set={working_set_tokens}")
            checkpoint_id = str(run.get("checkpoint_id", "")).strip()
            if checkpoint_id:
                parts.append(f"checkpoint={checkpoint_id}")
            if bool(run.get("pending_approval", False)):
                parts.append("approval=pending")
            if not parts:
                return ""
            return " · " + " · ".join(parts)

        items = "\n".join(
            (
                "<li>"
                '<div class="stack">'
                f'<a href="{escape(str(run.get("dashboard_href", "index.html")))}">'
                f"{escape(str(run.get('request', '(empty request)')))}</a>"
                f'<span class="muted">run_id={escape(str(run.get("run_id", "")))} '
                f"· intent={escape(str(run.get('intent', '(pending)')))} "
                f"· events={int(run.get('events_count', 0) or 0)} "
                f"· tools={int(run.get('tool_results_count', 0) or 0)}"
                f"{escape(_run_metadata_suffix(run))}</span>"
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
        items = "<li><span>No runs captured yet.</span></li>"

    cards = [
        _html_card(
            "Lula Trace Site",
            (
                "<p>Static dashboards generated from file-based run traces.</p>\n"
                '<p class="muted">Open a dashboard for graph, timeline,'
                " tool results, and final output.</p>"
            ),
        ),
        _html_card("Runs", f'<ul class="items">\n{items}\n</ul>'),
    ]
    return _html_document(
        title="Lula Trace Site",
        body="\n".join(cards),
        include_mermaid=False,
    )
