from __future__ import annotations

import json
from dataclasses import dataclass
from html import escape
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
    verification_raw = payload.get("verification", {})
    verification = verification_raw if isinstance(verification_raw, dict) else {}
    halt_reason = str(payload.get("halt_reason", "")).strip()
    final = str(payload.get("final", ""))

    events = [e for e in events_raw if isinstance(e, dict)]
    tool_results = [t for t in tools_raw if isinstance(t, dict)]
    final_lines = final.splitlines() or ["(empty)"]
    summary_lines: list[str] = []
    if "ok" in verification:
        summary_lines.append(
            f"verification: {'passed' if bool(verification.get('ok', False)) else 'failed'}"
        )
    if "acceptance_ok" in verification:
        summary_lines.append(
            f"acceptance: {'passed' if bool(verification.get('acceptance_ok', False)) else 'failed'}"
        )
    if halt_reason:
        summary_lines.append(f"halt_reason: {halt_reason}")
    if not summary_lines:
        summary_lines.append("No verification summary captured.")

    sections = [
        render_run_header(request=request, intent=intent),
        _box("Verification", summary_lines, width=width),
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


def render_run_viewer_spa(*, api_base_url: str = "", mermaid_graph: str = "") -> str:
    """
    Render a single-page application HTML that queries the live /v1/runs API.
    api_base_url: base URL for API calls (empty = same origin).
    """
    safe_base = api_base_url.rstrip("/")
    mermaid_script = ""
    if mermaid_graph:
        mermaid_script = (
            '<script type="module">\n'
            'import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs";\n'
            'mermaid.initialize({ startOnLoad: true, theme: "neutral" });\n'
            "</script>"
        )

    html_str = "".join(
        [
            "<!DOCTYPE html>\n",
            '<html lang="en">\n',
            "<head>\n",
            '  <meta charset="utf-8">\n',
            '  <meta name="viewport" content="width=device-width, initial-scale=1">\n',
            "  <title>LG Orchestration — Run Viewer</title>\n",
            "  <style>\n",
            "    :root { color-scheme: dark; }\n",
            "    * { box-sizing: border-box; }\n",
            "    body { margin: 0; background: #0f172a; color: #e2e8f0; font-family: Segoe UI, Arial, sans-serif; height: 100vh; display: flex; flex-direction: column; }\n",
            "    #layout { display: flex; flex: 1; overflow: hidden; }\n",
            "    #list-panel { width: 30%; border-right: 1px solid #334155; display: flex; flex-direction: column; overflow: hidden; }\n",
            "    #detail-panel { flex: 1; overflow-y: auto; padding: 16px; }\n",
            "    #submit-form { padding: 12px; border-bottom: 1px solid #334155; display: flex; gap: 8px; }\n",
            "    #submit-form input { flex: 1; background: #1e293b; border: 1px solid #334155; color: #e2e8f0; padding: 6px 10px; border-radius: 6px; font-size: 0.9rem; }\n",
            "    #submit-form button { background: #1d4ed8; color: #e2e8f0; border: none; padding: 6px 14px; border-radius: 6px; cursor: pointer; font-size: 0.9rem; white-space: nowrap; }\n",
            "    #submit-form button:hover { background: #2563eb; }\n",
            "    #run-list { flex: 1; overflow-y: auto; }\n",
            "    .run-card { padding: 10px 14px; border-bottom: 1px solid #1f2937; cursor: pointer; }\n",
            "    .run-card:hover { background: #1e293b; }\n",
            "    .run-card.selected { background: #1e293b; border-left: 3px solid #3b82f6; }\n",
            "    .run-id { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 0.75rem; color: #93c5fd; }\n",
            "    .run-req { font-size: 0.85rem; margin: 2px 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }\n",
            "    .run-meta { font-size: 0.75rem; color: #94a3b8; display: flex; gap: 8px; align-items: center; }\n",
            "    .badge { display: inline-block; padding: 1px 7px; border-radius: 999px; font-size: 0.72rem; text-align: center; }\n",
            "    .badge-running { background: #1e3a5f; color: #93c5fd; }\n",
            "    .badge-starting { background: #1e3a5f; color: #93c5fd; }\n",
            "    .badge-succeeded { background: #14532d; color: #bbf7d0; }\n",
            "    .badge-failed { background: #7f1d1d; color: #fecaca; }\n",
            "    .badge-cancelled { background: #374151; color: #9ca3af; }\n",
            "    .badge-cancelling { background: #374151; color: #9ca3af; }\n",
            "    .badge-ok { background: #14532d; color: #bbf7d0; }\n",
            "    .badge-err { background: #7f1d1d; color: #fecaca; }\n",
            "    .card { background: #111827; border: 1px solid #334155; border-radius: 10px; padding: 14px 16px; margin-bottom: 12px; }\n",
            "    .card h2 { margin: 0 0 10px; font-size: 1rem; color: #94a3b8; text-transform: uppercase; letter-spacing: 0.05em; }\n",
            "    .kv { display: grid; grid-template-columns: max-content 1fr; gap: 4px 12px; font-size: 0.85rem; }\n",
            "    .kv dt { color: #94a3b8; font-weight: 600; }\n",
            "    .kv dd { margin: 0; font-family: ui-monospace, SFMono-Regular, Consolas, monospace; color: #93c5fd; word-break: break-all; }\n",
            "    .tool-list { list-style: none; padding: 0; margin: 0; display: grid; gap: 6px; }\n",
            "    .tool-list li { display: flex; gap: 8px; align-items: flex-start; font-size: 0.85rem; }\n",
            "    .timeline-list { list-style: none; padding: 0; margin: 0; display: grid; gap: 4px; }\n",
            "    .timeline-list li { display: flex; gap: 10px; font-size: 0.82rem; }\n",
            "    .ts { color: #93c5fd; font-family: ui-monospace, SFMono-Regular, Consolas, monospace; min-width: 70px; }\n",
            "    pre { margin: 0; background: #0f172a; border: 1px solid #1f2937; border-radius: 6px; padding: 10px; font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 0.8rem; white-space: pre-wrap; overflow-x: auto; color: #e2e8f0; }\n",
            "    .diff-add { background: #14532d22; color: #bbf7d0; display: block; }\n",
            "    .diff-remove { background: #7f1d1d22; color: #fecaca; display: block; }\n",
            "    .diff-hunk { color: #94a3b8; font-style: italic; display: block; }\n",
            "    .action-bar { display: flex; gap: 8px; margin-bottom: 10px; }\n",
            "    .btn { background: #1e293b; border: 1px solid #334155; color: #e2e8f0; padding: 5px 12px; border-radius: 6px; cursor: pointer; font-size: 0.85rem; }\n",
            "    .btn:hover { background: #334155; }\n",
            "    .btn-danger { border-color: #7f1d1d; color: #fecaca; }\n",
            "    .btn-danger:hover { background: #7f1d1d44; }\n",
            "    #detail-empty { color: #475569; padding: 32px 16px; text-align: center; }\n",
            "    a { color: #93c5fd; text-decoration: none; }\n",
            "    a:hover { text-decoration: underline; }\n",
            "  </style>\n",
            "</head>\n",
            "<body>\n",
            '<div id="layout">\n',
            '  <div id="list-panel">\n',
            '    <form id="submit-form">\n',
            '      <input id="req-input" type="text" placeholder="Enter request..." autocomplete="off">\n',
            '      <button type="submit">Submit Request</button>\n',
            '    </form>\n',
            '    <div id="run-list"></div>\n',
            '  </div>\n',
            '  <div id="detail-panel"><div id="detail-empty">Select a run to view details.</div></div>\n',
            "</div>\n",
            "<script>\n",
            f'const API = "{safe_base}";\n',
            f'window.mermaidGraph = {json.dumps(mermaid_graph.strip()) if mermaid_graph else "null"};\n',
            r"""
let _selectedRunId = null;
let _listTimer = null;
let _detailTimer = null;
let _runs = [];

function statusBadge(status) {
  const cls = 'badge badge-' + (status || 'running');
  return `<span class="${cls}">${esc(status || 'unknown')}</span>`;
}

function esc(s) {
  if (s === null || s === undefined) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function fmtTime(iso) {
  if (!iso) return '';
  try { return new Date(iso).toLocaleTimeString(); } catch { return iso; }
}

function renderRunCard(run) {
  const sel = run.run_id === _selectedRunId ? ' selected' : '';
  const req = (run.request || '').slice(0, 60);
  return `<div class="run-card${sel}" data-id="${esc(run.run_id)}" onclick="selectRun('${esc(run.run_id)}')">
    <div class="run-id">${esc(run.run_id.slice(0, 16))}</div>
    <div class="run-req">${esc(req)}</div>
    <div class="run-meta">${statusBadge(run.status)}<span>${fmtTime(run.created_at)}</span></div>
  </div>`;
}

function renderList(runs) {
  const el = document.getElementById('run-list');
  el.innerHTML = runs.map(renderRunCard).join('');
}

function anyInProgress(runs) {
  return runs.some(r => r.status === 'running' || r.status === 'starting' || r.status === 'cancelling');
}

async function fetchList() {
  try {
    const res = await fetch(API + '/v1/runs');
    if (!res.ok) return;
    const data = await res.json();
    _runs = data.runs || [];
    renderList(_runs);
  } catch {}
  if (anyInProgress(_runs)) {
    _listTimer = setTimeout(fetchList, 2000);
  } else {
    _listTimer = null;
  }
}

function scheduleList() {
  if (_listTimer) clearTimeout(_listTimer);
  fetchList();
}

function selectRun(runId) {
  _selectedRunId = runId;
  document.querySelectorAll('.run-card').forEach(el => {
    el.classList.toggle('selected', el.dataset.id === runId);
  });
  if (_detailTimer) { clearTimeout(_detailTimer); _detailTimer = null; }
  loadDetail(runId);
}

function renderDiff(patch) {
  if (!patch) return '';
  const lines = patch.split('\n').map(line => {
    if (line.startsWith('@@')) return `<span class="diff-hunk">${esc(line)}</span>`;
    if (line.startsWith('+')) return `<span class="diff-add">${esc(line)}</span>`;
    if (line.startsWith('-')) return `<span class="diff-remove">${esc(line)}</span>`;
    return esc(line);
  });
  return `<pre>${lines.join('\n')}</pre>`;
}

function extractDiffs(toolResults) {
  const patches = [];
  for (const r of toolResults || []) {
    if (r.tool === 'apply_patch' && r.ok) {
      const inp = r.input || {};
      if (inp.patch) patches.push(inp.patch);
      if (Array.isArray(inp.changes)) {
        for (const c of inp.changes) { if (c.patch) patches.push(c.patch); }
      }
    }
  }
  return patches;
}

function renderDetail(run) {
  if (!run) { document.getElementById('detail-panel').innerHTML = '<div id="detail-empty">Run not found.</div>'; return; }
  const cancellable = run.cancellable;
  const inProgress = run.status === 'running' || run.status === 'starting' || run.status === 'cancelling';
  let html = `<div class="action-bar">
    <button class="btn" onclick="loadLogs('${esc(run.run_id)}')">View Logs</button>
    ${cancellable ? `<button class="btn btn-danger" onclick="cancelRun('${esc(run.run_id)}')">Cancel</button>` : ''}
  </div>`;

  html += `<div class="card"><h2>Run Info</h2><dl class="kv">
    <dt>run_id</dt><dd>${esc(run.run_id)}</dd>
    <dt>status</dt><dd>${statusBadge(run.status)}</dd>
    <dt>request</dt><dd>${esc(run.request)}</dd>
    <dt>intent</dt><dd>${esc(run.intent || '(pending)')}</dd>
    <dt>exit_code</dt><dd>${run.exit_code !== null && run.exit_code !== undefined ? esc(String(run.exit_code)) : '—'}</dd>
    <dt>trace_path</dt><dd>${esc(run.trace_path || '')}</dd>
    <dt>created_at</dt><dd>${esc(run.created_at || '')}</dd>
  </dl></div>`;

  if (run.trace_ready && run.trace) {
    const t = run.trace;
    const state = t.state || t;
    const events = (state.events || []).slice(-20);
    const tools = (state.tool_results || []).slice(-20);
    const finalOut = state.final || t.final || '';

    if (finalOut) {
      html += `<div class="card"><h2>Final Output</h2><pre>${esc(finalOut)}</pre></div>`;
    }

    if (events.length) {
      const startMs = events[0].ts_ms || 0;
      const items = events.map(e => {
        const delta = ((e.ts_ms || startMs) - startMs) / 1000;
        return `<li><span class="ts">+${delta.toFixed(2)}s</span><span>${esc(e.kind || 'event')}</span></li>`;
      }).join('');
      html += `<div class="card"><h2>Timeline</h2><ul class="timeline-list">${items}</ul></div>`;
    }

    if (tools.length) {
      const items = tools.map(r => {
        const badge = r.ok ? '<span class="badge badge-ok">OK</span>' : '<span class="badge badge-err">ERR</span>';
        return `<li>${badge}<span>${esc(r.tool || 'unknown')}</span></li>`;
      }).join('');
      html += `<div class="card"><h2>Tool Results</h2><ul class="tool-list">${items}</ul></div>`;
    }

    const diffs = extractDiffs(state.tool_results || t.tool_results || []);
    if (diffs.length) {
      const diffHtml = diffs.map(p => renderDiff(p)).join('<hr style="border-color:#1f2937;margin:8px 0;">');
      html += `<div class="card"><h2>Inline Diff</h2>${diffHtml}</div>`;
    }
  }

  if (window.mermaidGraph) {
      html += `<div class="card"><h2>Graph</h2><pre class="mermaid">${esc(window.mermaidGraph)}</pre></div>`;
  }

  html += `<div id="logs-section"></div>`;
  document.getElementById('detail-panel').innerHTML = html;
  
  if (window.mermaid) {
      setTimeout(() => window.mermaid.run(), 50);
  }

  if (inProgress) {
    _detailTimer = setTimeout(() => loadDetail(run.run_id), 2000);
  }
}

async function loadDetail(runId) {
  try {
    const res = await fetch(API + '/v1/runs/' + encodeURIComponent(runId));
    if (!res.ok) { renderDetail(null); return; }
    const run = await res.json();
    renderDetail(run);
    // refresh list too
    scheduleList();
  } catch { renderDetail(null); }
}

async function loadLogs(runId) {
  try {
    const res = await fetch(API + '/v1/runs/' + encodeURIComponent(runId) + '/logs');
    if (!res.ok) return;
    const data = await res.json();
    const sec = document.getElementById('logs-section');
    if (sec) {
      sec.innerHTML = `<div class="card"><h2>Logs</h2><pre>${esc((data.logs || []).join('\n'))}</pre></div>`;
    }
  } catch {}
}

async function cancelRun(runId) {
  try {
    await fetch(API + '/v1/runs/' + encodeURIComponent(runId) + '/cancel', { method: 'POST' });
    loadDetail(runId);
    scheduleList();
  } catch {}
}

document.getElementById('submit-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const input = document.getElementById('req-input');
  const req = input.value.trim();
  if (!req) return;
  input.value = '';
  try {
    const res = await fetch(API + '/v1/runs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ request: req }),
    });
    if (!res.ok) return;
    const run = await res.json();
    scheduleList();
    if (run.run_id) {
      setTimeout(() => selectRun(run.run_id), 200);
    }
  } catch {}
});

scheduleList();
""",
            "</script>\n",
            "</body>\n",
            "</html>\n",
        ]
    )

    if mermaid_script:
        return html_str.replace("</body>\n", f"{mermaid_script}\n</body>\n")
    return html_str
