use leptos::prelude::*;
use leptos_router::hooks::use_params_map;

use crate::{
    api::{
        client::ApiConfig,
        sse::{connect_sse, RunState},
        types::ApprovalRequest,
    },
    components::{
        audit_trail::AuditTrail, diff_viewer::DiffViewer, pipeline_graph::PipelineGraph,
        run_stream::RunStream, split_pane::SplitPane, tabs::Tabs, verifier_panel::VerifierPanel,
    },
};

#[component]
pub fn RunDetailPage() -> impl IntoView {
    let params = use_params_map();
    let run_id = move || params.with(|p| p.get("id").unwrap_or_default());

    let config = use_context::<ApiConfig>().unwrap();
    let approval_signal = use_context::<RwSignal<Option<ApprovalRequest>>>().unwrap();

    let current_id = run_id();
    let token = config.token.get_untracked();

    let (sse_state, cleanup) = connect_sse(&config.base_url, &current_id, token);
    on_cleanup(cleanup);

    let active_tab: RwSignal<usize> = RwSignal::new(0);

    // Watch for approval events in SSE state and propagate to global modal
    let id_for_effect = current_id.clone();
    Effect::new(move |_| {
        let state = sse_state.get();
        if let Some(mut approval) = state.approval {
            if approval.run_id.is_empty() {
                approval.run_id = id_for_effect.clone();
            }
            approval_signal.set(Some(approval));
        }
    });

    let state_signal: Signal<RunState> = Signal::derive(move || sse_state.get());

    let left_panel = ViewFn::from(move || {
        view! {
            <div style="border-right:1px solid var(--border);display:flex;flex-direction:column;overflow:hidden;height:100%;">
                <div style="padding:16px;font-size:11px;color:var(--text-muted);letter-spacing:0.5px;font-weight:600;">
                    "LIVE AGENT"
                </div>
                <div style="flex:1;overflow-y:auto;padding:0 12px 12px 12px;">
                    <RunStream state=state_signal />
                    {move || {
                        let s = sse_state.get();
                        if !s.is_done {
                            view! {
                                <div style="display:flex;align-items:center;gap:8px;padding:12px 0;color:var(--text-muted);font-size:13px;">
                                    <span class="spinner" style="width:14px;height:14px;border:2px solid var(--border);border-top-color:var(--accent);border-radius:50%;animation:spin 0.8s linear infinite;"></span>
                                    "Streaming..."
                                </div>
                            }.into_any()
                        } else if let Some(output) = s.final_output {
                            view! {
                                <div style="margin-top:12px;padding:14px 16px;background:var(--bg-surface);border:1px solid var(--border);border-radius:8px;">
                                    <div style="font-size:11px;color:var(--text-muted);letter-spacing:0.5px;margin-bottom:8px;font-weight:600;">
                                        "FINAL OUTPUT"
                                    </div>
                                    <div style="font-size:13px;color:var(--text-primary);white-space:pre-wrap;font-family:monospace;">
                                        {output}
                                    </div>
                                </div>
                            }.into_any()
                        } else {
                            view! { <div></div> }.into_any()
                        }
                    }}
                </div>
            </div>
        }
    });

    let right_panel = ViewFn::from(move || {
        view! {
            <div style="padding:16px;overflow-y:auto;height:100%;">
                <Tabs
                    tabs=vec![
                        "Diff".to_string(),
                        "Graph".to_string(),
                        "Verify".to_string(),
                        "Audit".to_string(),
                    ]
                    active=active_tab
                >
                    {move || {
                        let tab = active_tab.get();
                        match tab {
                            0 => view! { <DiffViewer state=state_signal /> }.into_any(),
                            1 => view! { <PipelineGraph state=state_signal /> }.into_any(),
                            2 => view! { <VerifierPanel state=state_signal /> }.into_any(),
                            3 => view! { <AuditTrail state=state_signal /> }.into_any(),
                            _ => view! { <div></div> }.into_any(),
                        }
                    }}
                </Tabs>
            </div>
        }
    });

    view! {
        <SplitPane initial_left_width=50.0 left=left_panel right=right_panel />
    }
}
