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
    """Render the SOTA 2026 single-page application that queries the live /v1/runs API.

    Features:
    - SSE live streaming timeline — each graph node pulses as it activates
    - Animated Mermaid graph with active-node highlighting via custom CSS overlay
    - GitHub-style syntax-highlighted unified diff for apply_patch results
    - Verifier report panel with OK/error semantic color
    - Run history with full-text search
    - Approval buttons inline in activity stream
    - Design-system dark theme with smooth CSS transitions
    api_base_url: base URL for API calls (empty = same origin).
    """
    safe_base = api_base_url.rstrip("/")
    escaped_mermaid = escape(mermaid_graph.strip()) if mermaid_graph else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Lula — Run Console</title>
  <style>
    /* ── Design tokens ─────────────────────────────────────── */
    :root {{
      --bg:      #0d1117;
      --bg2:     #161b22;
      --bg3:     #21262d;
      --border:  #30363d;
      --text:    #e6edf3;
      --muted:   #8b949e;
      --accent:  #58a6ff;
      --ok:      #3fb950;
      --err:     #f85149;
      --warn:    #d29922;
      --lane-interactive: #6366f1;
      --lane-deep:        #0ea5e9;
      --lane-recovery:    #f59e0b;
      color-scheme: dark;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
      font-size: 13px;
      height: 100dvh;
      display: flex;
      flex-direction: column;
      overflow: hidden;
    }}
    /* ── Top bar ─────────────────────────────────────────────── */
    #topbar {{
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 0 16px;
      height: 48px;
      border-bottom: 1px solid var(--border);
      flex-shrink: 0;
      background: var(--bg2);
    }}
    #topbar .logo {{ font-weight: 700; font-size: 15px; letter-spacing: -.3px; color: var(--text); }}
    #topbar .logo span {{ color: var(--accent); }}
    #req-form {{ display: flex; gap: 8px; flex: 1; max-width: 700px; }}
    #req-input {{
      flex: 1;
      background: var(--bg3);
      border: 1px solid var(--border);
      border-radius: 6px;
      color: var(--text);
      padding: 5px 10px;
      font-size: 13px;
      outline: none;
      transition: border-color .15s;
    }}
    #req-input:focus {{ border-color: var(--accent); }}
    .btn {{
      background: var(--bg3);
      border: 1px solid var(--border);
      border-radius: 6px;
      color: var(--text);
      cursor: pointer;
      font-size: 12px;
      padding: 5px 12px;
      transition: background .12s, border-color .12s;
      white-space: nowrap;
    }}
    .btn:hover {{ background: #2d333b; border-color: var(--accent); }}
    .btn-primary {{ background: #1f6feb; border-color: #388bfd; color: #fff; }}
    .btn-primary:hover {{ background: #388bfd; }}
    .btn-danger {{ border-color: var(--err); color: var(--err); }}
    .btn-danger:hover {{ background: #2d1b1b; }}
    /* ── Main layout ─────────────────────────────────────────── */
    #layout {{ display: flex; flex: 1; overflow: hidden; }}
    /* ── Left panel: run list ───────────────────────────────── */
    #list-panel {{
      width: 280px;
      min-width: 200px;
      border-right: 1px solid var(--border);
      display: flex;
      flex-direction: column;
      overflow: hidden;
      background: var(--bg2);
    }}
    #search-wrap {{ padding: 8px; border-bottom: 1px solid var(--border); }}
    #search-input {{
      width: 100%;
      background: var(--bg3);
      border: 1px solid var(--border);
      border-radius: 5px;
      color: var(--text);
      padding: 4px 8px;
      font-size: 12px;
      outline: none;
    }}
    #search-input:focus {{ border-color: var(--accent); }}
    #run-list {{
      flex: 1;
      overflow-y: auto;
      padding: 4px 0;
    }}
    .run-card {{
      padding: 8px 12px;
      cursor: pointer;
      border-left: 3px solid transparent;
      transition: background .1s, border-color .1s;
    }}
    .run-card:hover {{ background: var(--bg3); }}
    .run-card.selected {{ background: var(--bg3); border-left-color: var(--accent); }}
    .run-card .rc-req {{
      font-size: 12px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      color: var(--text);
    }}
    .run-card .rc-meta {{
      display: flex;
      gap: 8px;
      align-items: center;
      margin-top: 3px;
    }}
    .run-card .rc-time {{ font-size: 10px; color: var(--muted); }}
    /* ── Right panel: detail ────────────────────────────────── */
    #detail-panel {{
      flex: 1;
      overflow-y: auto;
      padding: 16px 20px;
      display: grid;
      gap: 12px;
      align-content: start;
    }}
    #detail-empty {{
      display: flex;
      align-items: center;
      justify-content: center;
      height: 200px;
      color: var(--muted);
      font-size: 14px;
    }}
    /* ── Cards ───────────────────────────────────────────────── */
    .card {{
      background: var(--bg2);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 14px 16px;
    }}
    .card h2 {{
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: .06em;
      color: var(--muted);
      margin-bottom: 10px;
    }}
    /* ── Badges ──────────────────────────────────────────────── */
    .badge {{
      display: inline-block;
      padding: 1px 7px;
      border-radius: 999px;
      font-size: 10px;
      font-weight: 600;
      line-height: 18px;
    }}
    .badge-running  {{ background:#1a3a5c; color:#58a6ff; }}
    .badge-starting {{ background:#1a3a5c; color:#58a6ff; }}
    .badge-ok       {{ background:#122217; color:var(--ok); }}
    .badge-err      {{ background:#2d1b1b; color:var(--err); }}
    .badge-warn     {{ background:#2d2106; color:var(--warn); }}
    .badge-other    {{ background:var(--bg3); color:var(--muted); }}
    /* ── KV table ────────────────────────────────────────────── */
    dl.kv {{ display: grid; grid-template-columns: max-content 1fr; gap: 4px 12px; font-size: 12px; }}
    dl.kv dt {{ color: var(--muted); white-space: nowrap; }}
    dl.kv dd {{ color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    /* ── Timeline ────────────────────────────────────────────── */
    #live-timeline {{ list-style: none; display: grid; gap: 4px; }}
    #live-timeline li {{
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 5px 8px;
      background: var(--bg3);
      border-radius: 5px;
      font-size: 11px;
      transition: background .3s;
    }}
    #live-timeline li.node-active {{
      background: #1a2a3a;
      border-left: 2px solid var(--accent);
      animation: pulse 1.2s ease-in-out infinite;
    }}
    @keyframes pulse {{
      0%, 100% {{ opacity: 1; }}
      50%       {{ opacity: .65; }}
    }}
    .tl-dot {{
      width: 7px; height: 7px; border-radius: 50%;
      background: var(--muted); flex-shrink: 0;
      transition: background .3s;
    }}
    .tl-dot.active {{ background: var(--accent); }}
    .tl-dot.ok     {{ background: var(--ok); }}
    .tl-dot.err    {{ background: var(--err); }}
    .ts {{ color: var(--muted); font-size: 10px; width: 52px; flex-shrink: 0; font-family: ui-monospace, monospace; }}
    .tl-label {{ flex: 1; color: var(--text); }}
    /* ── Lane indicator ──────────────────────────────────────── */
    .lane-pill {{
      font-size: 10px; font-weight: 600;
      padding: 1px 7px; border-radius: 999px; text-transform: uppercase;
    }}
    .lane-interactive {{ background:#312e81; color:#a5b4fc; }}
    .lane-deep_planning {{ background:#0c2e48; color:#7dd3fc; }}
    .lane-recovery    {{ background:#422006; color:#fcd34d; }}
    /* ── Tool list ───────────────────────────────────────────── */
    .tool-list {{ list-style: none; display: grid; gap: 3px; }}
    .tool-list li {{ display: flex; align-items: center; gap: 8px; font-size: 11px; padding: 3px 0; }}
    /* ── Diff viewer ─────────────────────────────────────────── */
    .diff-wrap {{ overflow-x: auto; }}
    .diff-wrap pre {{
      font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
      font-size: 11px; line-height: 1.55;
      margin: 0; padding: 10px 12px;
      background: #0d1117; border-radius: 5px;
      border: 1px solid var(--border);
    }}
    .diff-add    {{ color: #3fb950; display: block; }}
    .diff-remove {{ color: #f85149; display: block; }}
    .diff-hunk   {{ color: #8b949e; display: block; }}
    /* ── Verifier panel ──────────────────────────────────────── */
    #verifier-report {{
      font-family: ui-monospace, monospace; font-size: 11px;
      background: #0d1117; border-radius: 5px; padding: 10px;
      border: 1px solid var(--border);
      max-height: 260px; overflow: auto;
    }}
    /* ── Graph ───────────────────────────────────────────────── */
    .mermaid-wrap {{
      background: #0d1117; border-radius: 5px; padding: 12px;
      border: 1px solid var(--border); overflow: auto;
    }}
    /* ── Approval banner ─────────────────────────────────────── */
    #approval-banner {{
      display: none;
      background: #2d2106;
      border: 1px solid var(--warn);
      border-radius: 7px;
      padding: 12px 16px;
      gap: 12px;
      align-items: center;
    }}
    #approval-banner.visible {{ display: flex; }}
    #approval-banner .ab-text {{ flex: 1; font-size: 12px; color: var(--warn); }}
    /* ── Logs ────────────────────────────────────────────────── */
    #logs-pre {{
      font-family: ui-monospace, monospace; font-size: 10px;
      background: #0d1117; border-radius: 5px; padding: 8px 10px;
      border: 1px solid var(--border);
      max-height: 200px; overflow: auto;
    }}
    /* ── Final output ────────────────────────────────────────── */
    #final-output {{
      font-size: 13px; line-height: 1.65;
      white-space: pre-wrap; word-break: break-word;
    }}
    /* ── Action bar ──────────────────────────────────────────── */
    .action-bar {{ display: flex; gap: 8px; align-items: center; margin-bottom: 4px; }}
    /* ── Scrollbar ───────────────────────────────────────────── */
    ::-webkit-scrollbar {{ width: 5px; height: 5px; }}
    ::-webkit-scrollbar-track {{ background: var(--bg); }}
    ::-webkit-scrollbar-thumb {{ background: var(--border); border-radius: 9px; }}
  </style>
</head>
<body>

<!-- TOP BAR -->
<div id="topbar">
  <div class="logo">Lu<span>la</span></div>
  <form id="req-form" onsubmit="return false;">
    <input id="req-input" type="text" placeholder="Describe a task for the agent…" autocomplete="off">
    <button class="btn btn-primary" onclick="submitRun()">▶ Run</button>
  </form>
</div>

<!-- MAIN LAYOUT -->
<div id="layout">

  <!-- LEFT: run list -->
  <div id="list-panel">
    <div id="search-wrap">
      <input id="search-input" type="text" placeholder="Search runs…" oninput="filterList()">
    </div>
    <div id="run-list"></div>
  </div>

  <!-- RIGHT: detail -->
  <div id="detail-panel">
    <div id="detail-empty">Select a run or submit a request.</div>
  </div>

</div>

<script type="module">
import mermaid from 'https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs';
mermaid.initialize({{ startOnLoad: false, theme: 'dark', darkMode: true,
  themeVariables: {{ background: '#0d1117', primaryColor: '#1f6feb',
    edgeLabelBackground: '#161b22', nodeBorder: '#30363d', lineColor: '#8b949e' }} }});
window._mermaid = mermaid;
window._mermaidGraph = {repr(escaped_mermaid)};
</script>

<script>
const API = {repr(safe_base)};

// ── State ────────────────────────────────────────────────────
let _runs = [];
let _selected = null;
let _listTimer = null;
let _detailTimer = null;
let _activeSSE = null;
let _searchQuery = '';

// ── Utilities ────────────────────────────────────────────────
function esc(s) {{
  return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}}
function statusBadge(s) {{
  const cls = s === 'succeeded' ? 'ok' : s === 'failed' ? 'err'
            : s === 'running' || s === 'starting' ? 'running'
            : s === 'cancelled' || s === 'cancelling' ? 'warn' : 'other';
  return `<span class="badge badge-${{cls}}">${{esc(s ?? '?')}}</span>`;
}}
function lanePill(lane) {{
  if (!lane) return '';
  return `<span class="lane-pill lane-${{esc(lane)}}">${{esc(lane)}}</span>`;
}}
function fmtTime(iso) {{
  if (!iso) return '';
  try {{ return new Date(iso).toLocaleTimeString(); }} catch {{ return iso; }}
}}
function isInProgress(s) {{
  return s === 'running' || s === 'starting' || s === 'cancelling';
}}

// ── Run list ─────────────────────────────────────────────────
async function fetchList() {{
  try {{
    const res = await fetch(API + '/v1/runs', {{ headers: bearerHeaders() }});
    if (!res.ok) return;
    const data = await res.json();
    _runs = (data.runs || []).reverse();
    renderList();
  }} catch {{}}
  if (_runs.some(r => isInProgress(r.status))) {{
    _listTimer = setTimeout(fetchList, 2000);
  }} else {{
    _listTimer = null;
  }}
}}

function filterList() {{
  _searchQuery = document.getElementById('search-input').value.toLowerCase();
  renderList();
}}

function renderList() {{
  const q = _searchQuery;
  const filtered = q ? _runs.filter(r =>
    (r.request || '').toLowerCase().includes(q) ||
    (r.run_id || '').includes(q) ||
    (r.status || '').includes(q)
  ) : _runs;
  const container = document.getElementById('run-list');
  container.innerHTML = filtered.map(r => {{
    const sel = _selected === r.run_id ? ' selected' : '';
    const reqSnip = (r.request || '').slice(0, 80);
    return `<div class="run-card${{sel}}" data-id="${{esc(r.run_id)}}" onclick="selectRun('${{esc(r.run_id)}}')">
      <div class="rc-req" title="${{esc(r.request)}}">${{esc(reqSnip)}}</div>
      <div class="rc-meta">${{statusBadge(r.status)}}<span class="rc-time">${{fmtTime(r.created_at)}}</span></div>
    </div>`;
  }}).join('');
}}

// ── Run selection + SSE detail ───────────────────────────────
function selectRun(runId) {{
  _selected = runId;
  // Close any previous SSE stream
  if (_activeSSE) {{ _activeSSE.close(); _activeSSE = null; }}
  if (_detailTimer) {{ clearTimeout(_detailTimer); _detailTimer = null; }}
  renderList();
  openSSEStream(runId);
}}

function openSSEStream(runId) {{
  const url = API + '/v1/runs/' + encodeURIComponent(runId) + '/stream';
  const es = new EventSource(url);
  _activeSSE = es;
  es.onmessage = (ev) => {{
    try {{
      const run = JSON.parse(ev.data);
      if (run.error) {{ renderDetail(null, runId); es.close(); return; }}
      renderDetail(run, runId);
      renderList(); // update badge in sidebar
      if (run.finished_at) {{ es.close(); _activeSSE = null; }}
    }} catch {{}}
  }};
  es.addEventListener('done', () => {{ es.close(); _activeSSE = null; }});
  es.onerror = () => {{
    es.close(); _activeSSE = null;
    // Fall back to one-shot fetch
    loadDetailOnce(runId);
  }};
}}

async function loadDetailOnce(runId) {{
  try {{
    const res = await fetch(API + '/v1/runs/' + encodeURIComponent(runId), {{ headers: bearerHeaders() }});
    if (res.ok) renderDetail(await res.json(), runId);
  }} catch {{}}
}}

// ── Detail renderer ──────────────────────────────────────────
function renderDetail(run, runId) {{
  const panel = document.getElementById('detail-panel');
  if (!run) {{
    panel.innerHTML = '<div id="detail-empty">Run not found.</div>';
    return;
  }}

  const inProgress = isInProgress(run.status);
  let html = '';

  // Action bar
  html += `<div class="action-bar">
    ${{run.cancellable ? `<button class="btn btn-danger" onclick="cancelRun('${{esc(run.run_id)}}')">✕ Cancel</button>` : ''}}
    <button class="btn" onclick="loadLogs('${{esc(run.run_id)}}')">Show Logs</button>
  </div>`;

  // Approval banner
  if (run.pending_approval) {{
    html += `<div id="approval-banner" class="visible">
      <div class="ab-text">⚠ Pending approval: ${{esc(run.pending_approval_summary || 'mutation plan awaiting approval')}}</div>
      <button class="btn" onclick="approveMutation()">✓ Approve</button>
      <button class="btn btn-danger" onclick="rejectMutation()">✕ Reject</button>
    </div>`;
  }}

  // Run info
  const t = (run.trace && (run.trace.state || run.trace)) || {{}};
  const route = t.route || {{}};
  const intent = (t.intent || run.intent || '').trim();
  const lane = (route.lane || '').trim();
  html += `<div class="card"><h2>Run</h2><dl class="kv">
    <dt>status</dt><dd>${{statusBadge(run.status)}} ${{inProgress ? '<span style="color:var(--muted);font-size:10px">live</span>' : ''}}</dd>
    <dt>request</dt><dd title="${{esc(run.request)}}">${{esc((run.request || '').slice(0,120))}}</dd>
    <dt>intent</dt><dd>${{esc(intent || '—')}} ${{lanePill(lane)}}</dd>
    <dt>run_id</dt><dd style="font-family:ui-monospace;font-size:10px">${{esc(run.run_id)}}</dd>
    <dt>started</dt><dd>${{fmtTime(run.started_at)}}</dd>
    ${{run.finished_at ? `<dt>finished</dt><dd>${{fmtTime(run.finished_at)}}</dd>` : ''}}
    ${{run.exit_code != null ? `<dt>exit_code</dt><dd>${{esc(String(run.exit_code))}}</dd>` : ''}}
  </dl></div>`;

  // Final output
  const finalOut = (t.final || '').trim();
  if (finalOut || !inProgress) {{
    html += `<div class="card"><h2>Final Output</h2>
      <div id="final-output">${{esc(finalOut || '(no output yet)')}}</div>
    </div>`;
  }}

  // Live timeline
  const events = (t.events || []).slice(-40);
  if (events.length || inProgress) {{
    const startMs = events[0]?.ts_ms || 0;
    const items = events.map(ev => {{
      const d = ((ev.ts_ms || startMs) - startMs) / 1000;
      const data = ev.data || {{}};
      const name = data.name || '';
      const phase = data.phase || '';
      const label = [ev.kind, name, phase].filter(Boolean).join(' / ');
      const isActive = inProgress && phase === 'start' && events[events.length - 1] === ev;
      const dotCls = isActive ? 'active' : (data.ok === false ? 'err' : (data.ok === true ? 'ok' : ''));
      const liCls = isActive ? ' node-active' : '';
      return `<li class="${{liCls}}">
        <span class="tl-dot ${{dotCls}}"></span>
        <span class="ts">+${{d.toFixed(2)}}s</span>
        <span class="tl-label">${{esc(label)}}</span>
      </li>`;
    }}).join('');
    html += `<div class="card"><h2>Timeline${{inProgress ? ' <span style="color:var(--accent);font-size:10px">● live</span>' : ''}}</h2>
      <ul id="live-timeline">${{items || '<li><span class="tl-dot"></span><span class="tl-label" style="color:var(--muted)">Waiting…</span></li>'}}</ul>
    </div>`;
  }}

  // Tool results
  const tools = (t.tool_results || []).slice(-30);
  if (tools.length) {{
    const items = tools.map(r => {{
      const ok = r.ok;
      const dot = `<span class="tl-dot ${{ok ? 'ok' : 'err'}}"></span>`;
      const label = r.tool || 'unknown';
      const exit = r.exit_code != null ? ` (exit ${{r.exit_code}})` : '';
      return `<li>${{dot}}<span>${{esc(label + exit)}}</span></li>`;
    }}).join('');
    html += `<div class="card"><h2>Tools (${{tools.length}})</h2><ul class="tool-list">${{items}}</ul></div>`;
  }}

  // Inline diffs
  const diffs = extractDiffs(t.tool_results || []);
  if (diffs.length) {{
    const diffHtml = diffs.map(p => renderDiff(p)).join('<hr style="border-color:var(--border);margin:6px 0">');
    html += `<div class="card"><h2>Patches</h2><div class="diff-wrap">${{diffHtml}}</div></div>`;
  }}

  // Verifier report
  const ver = t.verification || run.verification;
  if (ver) {{
    const verOk = ver.ok;
    const cls = verOk ? 'ok' : 'err';
    html += `<div class="card"><h2>Verifier ${{statusBadge(verOk ? 'succeeded' : 'failed')}}</h2>
      <pre id="verifier-report">${{esc(JSON.stringify(ver, null, 2))}}</pre>
    </div>`;
  }}

  // Graph
  if (window._mermaidGraph) {{
    html += `<div class="card"><h2>Graph</h2>
      <div class="mermaid-wrap">
        <div class="mermaid" id="mermaid-diagram">${{esc(window._mermaidGraph)}}</div>
      </div>
    </div>`;
  }}

  html += `<div id="logs-section"></div>`;
  panel.innerHTML = html;

  // Render mermaid + highlight active node
  if (window._mermaid && window._mermaidGraph) {{
    window._mermaid.run({{ nodes: [document.getElementById('mermaid-diagram')] }})
      .catch(() => {{}})
      .then(() => {{
        if (lane) highlightLane(lane);
        const lastNode = (events.filter(e => e.data?.phase === 'start').slice(-1)[0]?.data?.name || '');
        if (lastNode) highlightNode(lastNode);
      }});
  }}
}}

// ── Mermaid helpers ──────────────────────────────────────────
function highlightNode(nodeName) {{
  document.querySelectorAll('#mermaid-diagram .node').forEach(el => {{
    const label = el.querySelector('.label');
    if (label && label.textContent.trim() === nodeName) {{
      el.style.filter = 'drop-shadow(0 0 6px #58a6ff)';
    }}
  }});
}}
function highlightLane(lane) {{
  // Colour the background of the diagram wrapper by lane
  const colors = {{ interactive: '#1a1a3a', deep_planning: '#0a1e2a', recovery: '#1a1000' }};
  const wrap = document.querySelector('.mermaid-wrap');
  if (wrap && colors[lane]) wrap.style.background = colors[lane];
}}

// ── Diff renderer ────────────────────────────────────────────
function extractDiffs(toolResults) {{
  const patches = [];
  for (const r of toolResults || []) {{
    if (r.tool === 'apply_patch' && r.ok) {{
      const inp = r.input || {{}};
      if (inp.patch) patches.push(inp.patch);
      if (Array.isArray(inp.changes)) {{
        for (const c of inp.changes) {{ if (c.patch) patches.push(c.patch); }}
      }}
    }}
  }}
  return patches;
}}
function renderDiff(patch) {{
  if (!patch) return '';
  const lines = patch.split('\\n').map(line => {{
    if (line.startsWith('@@'))  return `<span class="diff-hunk">${{esc(line)}}</span>`;
    if (line.startsWith('+'))   return `<span class="diff-add">${{esc(line)}}</span>`;
    if (line.startsWith('-'))   return `<span class="diff-remove">${{esc(line)}}</span>`;
    return esc(line);
  }});
  return `<pre>${{lines.join('\\n')}}</pre>`;
}}

// ── Logs ─────────────────────────────────────────────────────
async function loadLogs(runId) {{
  try {{
    const res = await fetch(API + '/v1/runs/' + encodeURIComponent(runId) + '/logs', {{ headers: bearerHeaders() }});
    if (!res.ok) return;
    const data = await res.json();
    const sec = document.getElementById('logs-section');
    if (sec) {{
      sec.innerHTML = `<div class="card"><h2>Logs</h2><pre id="logs-pre">${{esc((data.logs || []).join('\\n'))}}</pre></div>`;
    }}
  }} catch {{}}
}}

// ── Actions ──────────────────────────────────────────────────
async function cancelRun(runId) {{
  try {{
    await fetch(API + '/v1/runs/' + encodeURIComponent(runId) + '/cancel',
      {{ method: 'POST', headers: bearerHeaders() }});
    fetchList();
  }} catch {{}}
}}

async function submitRun() {{
  const input = document.getElementById('req-input');
  const req = input.value.trim();
  if (!req) return;
  input.value = '';
  try {{
    const res = await fetch(API + '/v1/runs', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json', ...bearerHeaders() }},
      body: JSON.stringify({{ request: req }}),
    }});
    if (!res.ok) return;
    const run = await res.json();
    _runs.unshift(run);
    renderList();
    if (run.run_id) setTimeout(() => selectRun(run.run_id), 100);
    if (_listTimer) clearTimeout(_listTimer);
    _listTimer = setTimeout(fetchList, 2000);
  }} catch {{}}
}}

function approveMutation() {{ console.log('approve'); /* TODO: wire to approval token API */ }}
function rejectMutation() {{  console.log('reject'); }}

// ── Auth ─────────────────────────────────────────────────────
function bearerHeaders() {{
  const tok = (window._bearerToken || '').trim();
  return tok ? {{ Authorization: 'Bearer ' + tok }} : {{}};
}}
// Expose for devtools: window._bearerToken = 'your-token'

// ── Bootstrap ────────────────────────────────────────────────
window.selectRun = selectRun;
window.cancelRun = cancelRun;
window.loadLogs = loadLogs;
window.submitRun = submitRun;
window.approveMutation = approveMutation;
window.rejectMutation = rejectMutation;
window.filterList = filterList;

document.getElementById('req-input').addEventListener('keydown', e => {{
  if (e.key === 'Enter') submitRun();
}});

fetchList();
</script>
</body>
</html>"""
