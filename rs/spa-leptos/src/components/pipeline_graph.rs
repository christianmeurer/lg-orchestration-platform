use leptos::prelude::*;

use crate::api::sse::RunState;

const PIPELINE_STAGES: [&str; 9] = [
    "ingest",
    "router",
    "policy_gate",
    "planner",
    "context_builder",
    "coder",
    "executor",
    "verifier",
    "reporter",
];

#[derive(Debug, Clone, Copy, PartialEq)]
enum StageState {
    Pending,
    Active,
    Done,
    Error,
}

fn compute_stage_states(state: &RunState) -> Vec<(&'static str, StageState)> {
    PIPELINE_STAGES
        .iter()
        .map(|&stage| {
            let has_start = state.events.iter().any(|ev| {
                ev.node_name() == stage
                    && ev.kind.as_deref() == Some("node")
                    && ev.phase().as_deref() == Some("start")
            });
            let has_end = state.events.iter().any(|ev| {
                ev.node_name() == stage
                    && ev.kind.as_deref() == Some("node")
                    && ev.phase().as_deref() == Some("end")
            });
            let has_error = state
                .events
                .iter()
                .any(|ev| ev.node_name() == stage && ev.kind.as_deref() == Some("error"));

            let s = if has_error {
                StageState::Error
            } else if has_end {
                StageState::Done
            } else if has_start {
                StageState::Active
            } else {
                StageState::Pending
            };

            (stage, s)
        })
        .collect()
}

#[component]
pub fn PipelineGraph(#[prop(into)] state: Signal<RunState>) -> impl IntoView {
    view! {
        <div style="display:flex;flex-direction:column;gap:4px;">
            {move || {
                let stages = compute_stage_states(&state.get());
                stages
                    .into_iter()
                    .map(|(name, stage_state)| {
                        let (dot_color, text_color) = match stage_state {
                            StageState::Done => ("var(--ok)", "var(--ok)"),
                            StageState::Active => ("var(--accent)", "var(--accent)"),
                            StageState::Error => ("var(--err)", "var(--err)"),
                            StageState::Pending => ("var(--border)", "var(--text-muted)"),
                        };
                        let active_class = if stage_state == StageState::Active {
                            " pipeline-active"
                        } else {
                            ""
                        };
                        view! {
                            <div
                                class=format!("pipeline-stage{}", active_class)
                                style="display:flex;align-items:center;gap:10px;padding:6px 12px;"
                            >
                                <span style=format!(
                                    "width:8px;height:8px;border-radius:50%;background:{};flex-shrink:0;",
                                    dot_color,
                                )></span>
                                <span style=format!(
                                    "font-size:13px;font-weight:500;color:{};",
                                    text_color,
                                )>
                                    {name}
                                </span>
                            </div>
                        }
                    })
                    .collect::<Vec<_>>()
            }}
        </div>
    }
}
