use leptos::prelude::*;

use crate::api::{sse::RunState, types::TraceEvent};

#[derive(Debug, Clone)]
struct NodeGroup {
    name: String,
    events: Vec<TraceEvent>,
    done: bool,
}

fn build_groups(events: &[TraceEvent]) -> Vec<NodeGroup> {
    let mut groups: Vec<NodeGroup> = Vec::new();
    for ev in events {
        let node_name = ev.node.clone().unwrap_or_else(|| "unknown".to_string());
        if let Some(group) = groups.iter_mut().find(|g| g.name == node_name) {
            if ev.kind.as_deref() == Some("node_end") {
                group.done = true;
            }
            group.events.push(ev.clone());
        } else {
            let done = ev.kind.as_deref() == Some("node_end");
            groups.push(NodeGroup { name: node_name, events: vec![ev.clone()], done });
        }
    }
    groups
}

fn event_color(kind: Option<&str>) -> &'static str {
    match kind {
        Some("tool_call") => "var(--text-faint)",
        Some("tool_result") => "var(--ok)",
        Some("llm_chunk") => "var(--text-secondary)",
        Some("error") => "var(--err)",
        _ => "var(--text-muted)",
    }
}

#[component]
pub fn RunStream(#[prop(into)] state: Signal<RunState>) -> impl IntoView {
    view! {
        <div style="display:flex;flex-direction:column;gap:4px;">
            {move || {
                let s = state.get();
                let groups = build_groups(&s.events);
                groups
                    .into_iter()
                    .map(|group| {
                        view! {
                            <NodeSection group=group />
                        }
                    })
                    .collect::<Vec<_>>()
            }}
            <StdoutPanel state=state />
        </div>
    }
}

#[component]
fn NodeSection(group: NodeGroup) -> impl IntoView {
    let (expanded, set_expanded) = signal(true);
    let name = group.name.clone();
    let done = group.done;
    let tool_count = group.events.iter().filter(|e| e.kind.as_deref() == Some("tool_call")).count();

    let elapsed: u64 = {
        let timestamps: Vec<u64> = group.events.iter().filter_map(|e| e.ts_ms).collect();
        if timestamps.len() >= 2 {
            timestamps.last().unwrap_or(&0) - timestamps.first().unwrap_or(&0)
        } else {
            0
        }
    };

    let events = group.events;

    view! {
        <div style="border:1px solid var(--border);border-radius:6px;overflow:hidden;">
            <div
                on:click=move |_| set_expanded.set(!expanded.get())
                style="display:flex;align-items:center;gap:8px;padding:8px 12px;background:var(--bg-elevated);cursor:pointer;user-select:none;"
            >
                <span style="font-size:10px;color:var(--text-muted);width:12px;">
                    {move || if expanded.get() { "\u{25BE}" } else { "\u{25B8}" }}
                </span>
                <span style="font-size:13px;color:var(--text-primary);font-weight:500;">
                    {name}
                </span>
                <span style="font-size:12px;">
                    {if done { "\u{2713}" } else { "\u{25CF}" }}
                </span>
                {if tool_count > 0 {
                    Some(view! {
                        <span style="font-size:11px;color:var(--text-muted);margin-left:auto;">
                            {format!("{} tools", tool_count)}
                        </span>
                    })
                } else {
                    None
                }}
                {if elapsed > 0 {
                    Some(view! {
                        <span style="font-size:11px;color:var(--text-faint);">
                            {format!("{}ms", elapsed)}
                        </span>
                    })
                } else {
                    None
                }}
            </div>
            <div style:display=move || if expanded.get() { "block" } else { "none" }>
                <div style="padding:8px 12px;display:flex;flex-direction:column;gap:2px;">
                    {events
                        .iter()
                        .map(|ev| {
                            let kind_str = ev.kind.clone().unwrap_or_default();
                            let color = event_color(ev.kind.as_deref());
                            let data_preview = ev
                                .data
                                .as_ref()
                                .map(|d| {
                                    let s = d.to_string();
                                    if s.len() > 120 {
                                        format!("{}...", &s[..120])
                                    } else {
                                        s
                                    }
                                })
                                .unwrap_or_default();
                            view! {
                                <div style=format!(
                                    "font-size:12px;font-family:monospace;color:{};padding:2px 0;",
                                    color,
                                )>
                                    <span style="opacity:0.6;margin-right:8px;">
                                        {kind_str}
                                    </span>
                                    {data_preview}
                                </div>
                            }
                        })
                        .collect::<Vec<_>>()}
                </div>
            </div>
        </div>
    }
}

#[component]
fn StdoutPanel(#[prop(into)] state: Signal<RunState>) -> impl IntoView {
    view! {
        <div style="margin-top:8px;">
            {move || {
                let lines = state.get().stdout_lines;
                if lines.is_empty() {
                    None
                } else {
                    Some(view! {
                        <div style="background:var(--bg-void);border:1px solid var(--border);border-radius:6px;padding:10px 12px;">
                            <div style="font-size:11px;color:var(--text-muted);margin-bottom:6px;letter-spacing:0.5px;">
                                "STDOUT"
                            </div>
                            {lines
                                .iter()
                                .map(|l| {
                                    let tool = l.tool.clone();
                                    let line = l.line.clone();
                                    view! {
                                        <div style="font-size:12px;font-family:monospace;color:var(--text-secondary);padding:1px 0;">
                                            <span style="color:var(--text-faint);margin-right:8px;">
                                                {format!("[{}]", tool)}
                                            </span>
                                            {line}
                                        </div>
                                    }
                                })
                                .collect::<Vec<_>>()}
                        </div>
                    })
                }
            }}
        </div>
    }
}
