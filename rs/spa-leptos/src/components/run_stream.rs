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
        let node_name = ev.node_name();
        let is_end = ev.kind.as_deref() == Some("node") && ev.phase().as_deref() == Some("end");
        if let Some(group) = groups.iter_mut().find(|g| g.name == node_name) {
            if is_end {
                group.done = true;
            }
            group.events.push(ev.clone());
        } else {
            groups.push(NodeGroup { name: node_name, events: vec![ev.clone()], done: is_end });
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

                if !groups.is_empty() && groups.iter().any(|g| g.name != "unknown") {
                    // Structured trace events available — show node groups
                    groups
                        .into_iter()
                        .map(|group| view! { <NodeSection group=group /> }.into_any())
                        .collect::<Vec<_>>()
                } else if !s.log_lines.is_empty() {
                    // No structured events but log lines from run summary
                    vec![view! {
                        <div class="terminal-window">
                            <div class="terminal-titlebar">
                                <span class="terminal-dot red"></span>
                                <span class="terminal-dot yellow"></span>
                                <span class="terminal-dot green"></span>
                            </div>
                            <div class="terminal-body">
                                {s.log_lines.iter().map(|line| {
                                    let css_class = if line.contains("error") || line.contains("ERROR") || line.contains("FAIL") {
                                        "log-line-error"
                                    } else if line.contains("warning") || line.contains("WARNING") {
                                        "log-line-warning"
                                    } else if line.starts_with('[') && line.contains(']') {
                                        "log-line-step"
                                    } else if line.starts_with('\u{256D}') || line.starts_with('\u{2502}') || line.starts_with('\u{2570}') || line.starts_with('\u{2500}') {
                                        "log-line-box"
                                    } else {
                                        ""
                                    };
                                    view! { <div class=css_class>{line.clone()}</div> }
                                }).collect::<Vec<_>>()}
                            </div>
                        </div>
                    }.into_any()]
                } else {
                    vec![view! {
                        <div class="empty-state" style="padding:40px 20px;">
                            <div class="empty-state-icon">"\u{1F4E1}"</div>
                            <div class="empty-state-text">"Waiting for events..."</div>
                        </div>
                    }.into_any()]
                }
            }}
            {move || {
                let s = state.get();
                if !s.is_done && (!s.log_lines.is_empty() || !s.events.is_empty()) {
                    Some(view! {
                        <div style="display:flex;align-items:center;gap:8px;padding:12px 0;color:var(--text-muted);font-size:13px;">
                            <span class="streaming-dots">
                                <span></span>
                                <span></span>
                                <span></span>
                            </span>
                            "Streaming"
                        </div>
                    })
                } else {
                    None
                }
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
