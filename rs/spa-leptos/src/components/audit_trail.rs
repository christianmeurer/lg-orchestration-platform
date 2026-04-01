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
        Some("node_start") | Some("node_end") => "var(--info)",
        _ => "var(--text-muted)",
    }
}

#[component]
pub fn AuditTrail(#[prop(into)] state: Signal<RunState>) -> impl IntoView {
    view! {
        <div style="font-family:monospace;font-size:12px;">
            <div style="display:grid;grid-template-columns:80px 120px 1fr;gap:0;border-bottom:1px solid var(--border);padding-bottom:4px;margin-bottom:4px;">
                <span style="color:var(--text-muted);font-weight:600;">"TIME"</span>
                <span style="color:var(--text-muted);font-weight:600;">"NODE"</span>
                <span style="color:var(--text-muted);font-weight:600;">"KIND"</span>
            </div>
            {move || {
                let s = state.get();
                s.events
                    .iter()
                    .map(|ev| {
                        let ts = format_timestamp(ev.ts_ms);
                        let node = ev.node.clone().unwrap_or_else(|| "--".to_string());
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
                    .collect::<Vec<_>>()
            }}
        </div>
    }
}
