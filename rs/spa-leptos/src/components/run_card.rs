use leptos::prelude::*;

use super::status_badge::StatusBadge;
use crate::api::types::{RunStatus, RunSummary};

const PIPELINE_STAGES: [&str; 7] =
    ["ingest", "router", "planner", "coder", "executor", "verifier", "reporter"];

fn format_elapsed(ms: Option<u64>) -> String {
    match ms {
        None => "--".to_string(),
        Some(ms) if ms < 1000 => format!("{}ms", ms),
        Some(ms) if ms < 60_000 => format!("{:.1}s", ms as f64 / 1000.0),
        Some(ms) => format!("{:.1}m", ms as f64 / 60_000.0),
    }
}

fn stage_index(current_node: &Option<String>) -> usize {
    match current_node {
        None => 0,
        Some(node) => {
            PIPELINE_STAGES.iter().position(|s| node.contains(s)).map(|i| i + 1).unwrap_or(0)
        }
    }
}

#[component]
pub fn RunCard(run: RunSummary, #[prop(optional)] selected: bool) -> impl IntoView {
    let is_active = run.status == RunStatus::Running;
    let active_class = if is_active { " run-card-active" } else { "" };
    let border = if selected {
        "border:1px solid var(--accent);"
    } else {
        "border:1px solid var(--border);"
    };

    let truncated_request = if run.request.len() > 50 {
        format!("{}...", &run.request[..50])
    } else {
        run.request.clone()
    };

    let elapsed_text = format_elapsed(run.elapsed_ms);
    let progress = stage_index(&run.current_node);
    let pending = run.pending_approval;
    let status = run.status.clone();

    view! {
        <div
            class=format!("run-card{}", active_class)
            style=format!(
                "{}background:var(--bg-surface);border-radius:8px;padding:14px 16px;cursor:pointer;transition:border-color 0.15s;",
                border,
            )
        >
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
                <span style="font-size:13px;color:var(--text-primary);font-weight:500;">
                    {truncated_request}
                </span>
                <StatusBadge status=status pending_approval=pending />
            </div>
            <div style="display:flex;gap:2px;height:4px;margin-bottom:8px;">
                {PIPELINE_STAGES
                    .iter()
                    .enumerate()
                    .map(|(i, _)| {
                        let color = if i < progress {
                            "var(--accent)"
                        } else {
                            "var(--border)"
                        };
                        view! {
                            <div style=format!(
                                "flex:1;border-radius:2px;background:{};transition:background 0.3s;",
                                color,
                            )></div>
                        }
                    })
                    .collect::<Vec<_>>()}
            </div>
            <div style="font-size:11px;color:var(--text-muted);">
                {elapsed_text}
            </div>
        </div>
    }
}
