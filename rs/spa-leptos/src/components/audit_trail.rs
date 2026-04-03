use leptos::prelude::*;

use crate::api::sse::RunState;

fn format_timestamp(ts_ms: Option<u64>) -> String {
    match ts_ms {
        None => "--:--:--".to_string(),
        Some(ms) => {
            let total_secs = ms / 1000;
            let h = (total_secs / 3600) % 24;
            let m = (total_secs % 3600) / 60;
            let s = total_secs % 60;
            format!("{:02}:{:02}:{:02}", h, m, s)
        }
    }
}

fn kind_color(kind: Option<&str>) -> &'static str {
    match kind {
        Some("error") => "var(--err)",
        Some("tool_call") => "var(--text-faint)",
        Some("tool_result") => "var(--ok)",
        Some("node_start") | Some("node_end") | Some("node") => "var(--info)",
        _ => "var(--text-muted)",
    }
}

#[component]
pub fn AuditTrail(#[prop(into)] state: Signal<RunState>) -> impl IntoView {
    view! {
        <div style="display:flex;flex-direction:column;gap:16px;">
            // Pipeline nodes table (from parsed log lines)
            {move || {
                let s = state.get();
                let nodes = &s.pipeline_nodes;
                if nodes.is_empty() && s.events.is_empty() {
                    let msg = if s.is_done {
                        "No audit data available."
                    } else {
                        "Audit trail available after completion."
                    };
                    return view! {
                        <div class="empty-state" style="padding:40px 20px;">
                            <div style="font-size:28px;opacity:0.2;margin-bottom:12px;">"\u{1F4DC}"</div>
                            <div style="font-size:13px;color:var(--text-muted);">{msg}</div>
                        </div>
                    }
                    .into_any();
                }

                let has_nodes = !nodes.is_empty();
                let has_events = !s.events.is_empty();

                let nodes_section = if has_nodes {
                    let node_rows: Vec<_> = nodes
                        .iter()
                        .map(|node| {
                            let status_icon = if node.done { "\u{2713}" } else { "\u{25CF}" };
                            let status_color = if node.done {
                                "var(--ok)"
                            } else {
                                "var(--accent)"
                            };
                            let idx = format!("{:02}", node.index);
                            let name = node.name.clone();
                            let events = format!("{}", node.events);
                            let tools = format!("{}", node.tools);
                            view! {
                                <div style="display:grid;grid-template-columns:32px 24px 1fr 60px 60px;gap:8px;padding:6px 12px;align-items:center;border-radius:var(--radius-sm);background:var(--bg-elevated);">
                                    <span style="font-size:11px;color:var(--text-faint);font-family:var(--font-mono);">
                                        {idx}
                                    </span>
                                    <span style=format!("color:{};font-size:12px;", status_color)>
                                        {status_icon}
                                    </span>
                                    <span style="font-size:13px;color:var(--text-primary);font-weight:500;">
                                        {name}
                                    </span>
                                    <span style="font-size:11px;color:var(--text-muted);text-align:right;">
                                        {events}" evt"
                                    </span>
                                    <span style="font-size:11px;color:var(--text-muted);text-align:right;">
                                        {tools}" tools"
                                    </span>
                                </div>
                            }
                        })
                        .collect();
                    Some(view! {
                        <div>
                            <div style="font-size:11px;color:var(--text-muted);letter-spacing:0.5px;font-weight:600;margin-bottom:8px;">
                                "PIPELINE NODES"
                            </div>
                            <div style="display:flex;flex-direction:column;gap:2px;">
                                {node_rows}
                            </div>
                        </div>
                    })
                } else {
                    None
                };

                let events_section = if has_events {
                    let event_rows: Vec<_> = s
                        .events
                        .iter()
                        .map(|ev| {
                            let ts = format_timestamp(ev.ts_ms);
                            let node = ev.node_name();
                            let kind = ev.kind.clone().unwrap_or_else(|| "--".to_string());
                            let color = kind_color(ev.kind.as_deref());
                            view! {
                                <div style="display:grid;grid-template-columns:80px 120px 1fr;gap:0;padding:2px 0;">
                                    <span style="color:var(--text-faint);">{ts}</span>
                                    <span style="color:var(--text-secondary);">{node}</span>
                                    <span style=format!("color:{};", color)>{kind}</span>
                                </div>
                            }
                        })
                        .collect();
                    Some(view! {
                        <div>
                            <div style="font-size:11px;color:var(--text-muted);letter-spacing:0.5px;font-weight:600;margin-bottom:8px;">
                                "TRACE EVENTS"
                            </div>
                            <div style="font-family:monospace;font-size:12px;">
                                <div style="display:grid;grid-template-columns:80px 120px 1fr;gap:0;border-bottom:1px solid var(--border);padding-bottom:4px;margin-bottom:4px;">
                                    <span style="color:var(--text-muted);font-weight:600;">"TIME"</span>
                                    <span style="color:var(--text-muted);font-weight:600;">"NODE"</span>
                                    <span style="color:var(--text-muted);font-weight:600;">"KIND"</span>
                                </div>
                                {event_rows}
                            </div>
                        </div>
                    })
                } else {
                    None
                };

                view! {
                    <div style="display:flex;flex-direction:column;gap:20px;">
                        {nodes_section}
                        {events_section}
                    </div>
                }
                .into_any()
            }}
        </div>
    }
}
