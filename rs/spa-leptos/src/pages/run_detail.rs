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
        run_stream::RunStream, tabs::Tabs, verifier_panel::VerifierPanel,
    },
};

fn status_badge_class(status: &Option<String>, is_done: bool) -> &'static str {
    match status.as_deref() {
        Some("succeeded") => "badge badge-passed",
        Some("failed") | Some("cancelled") => "badge badge-failed",
        Some("suspended") => "badge badge-suspended",
        _ if is_done => "badge badge-passed",
        _ => "badge badge-running",
    }
}

fn status_badge_text(status: &Option<String>, is_done: bool) -> &'static str {
    match status.as_deref() {
        Some("succeeded") => "COMPLETED",
        Some("failed") => "FAILED",
        Some("cancelled") => "CANCELLED",
        Some("suspended") => "SUSPENDED",
        _ if is_done => "COMPLETED",
        _ => "RUNNING",
    }
}

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

    // Elapsed time counter (seconds since page load)
    let (elapsed, set_elapsed) = signal(0u64);
    {
        leptos::task::spawn_local(async move {
            loop {
                gloo_timers::future::TimeoutFuture::new(1_000).await;
                set_elapsed.update(|e| *e += 1);
            }
        });
    }

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

    let display_id = current_id.clone();

    view! {
        <div class="run-detail-layout">
            // ── Top section: task description, status, elapsed ──
            <div class="run-detail-header">
                <div class="run-detail-header-left">
                    <div class="run-detail-request">
                        {move || {
                            let s = sse_state.get();
                            s.request
                                .unwrap_or_else(|| format!("Run {}", display_id))
                        }}
                    </div>
                    <div style="display:flex;align-items:center;gap:12px;margin-top:8px;">
                        {move || {
                            let s = sse_state.get();
                            let cls = status_badge_class(&s.status, s.is_done);
                            let txt = status_badge_text(&s.status, s.is_done);
                            view! { <span class=cls>{txt}</span> }
                        }}
                        <span style="font-size:12px;color:var(--text-muted);font-family:var(--font-mono);">
                            {move || {
                                let secs = elapsed.get();
                                let m = secs / 60;
                                let s = secs % 60;
                                format!("{:02}:{:02}", m, s)
                            }}
                        </span>
                    </div>
                </div>
            </div>

            // ── Pipeline visualization ──
            <div class="run-detail-pipeline">
                <PipelineGraph state=state_signal />
            </div>

            // ── Two-panel layout: terminal + tabs ──
            <div class="run-detail-panels">
                // Left: terminal log output
                <div class="run-detail-terminal-panel">
                    <div style="padding:10px 14px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:10px;background:var(--bg-surface);">
                        <div style="font-size:11px;color:var(--text-muted);letter-spacing:0.5px;font-weight:600;">
                            "LIVE AGENT"
                        </div>
                        {move || {
                            let s = sse_state.get();
                            if !s.is_done {
                                Some(view! {
                                    <span class="streaming-dots" style="margin-left:auto;">
                                        <span></span>
                                        <span></span>
                                        <span></span>
                                    </span>
                                })
                            } else {
                                None
                            }
                        }}
                    </div>
                    <div style="flex:1;overflow-y:auto;padding:12px;">
                        <RunStream state=state_signal />
                        {move || {
                            let s = sse_state.get();
                            if s.is_done {
                                s.final_output.map(|output| {
                                    view! {
                                        <div style="margin-top:12px;padding:14px 16px;background:var(--bg-surface);border:1px solid var(--border);border-radius:8px;">
                                            <div style="font-size:11px;color:var(--text-muted);letter-spacing:0.5px;margin-bottom:8px;font-weight:600;">
                                                "FINAL OUTPUT"
                                            </div>
                                            <div style="font-size:13px;color:var(--text-primary);white-space:pre-wrap;font-family:monospace;">
                                                {output}
                                            </div>
                                        </div>
                                    }
                                })
                            } else {
                                None
                            }
                        }}
                    </div>
                </div>

                // Right: tabbed panel
                <div class="run-detail-tabs-panel">
                    <Tabs
                        tabs=vec![
                            "Diff".to_string(),
                            "Verify".to_string(),
                            "Audit".to_string(),
                        ]
                        active=active_tab
                    >
                        {move || {
                            let tab = active_tab.get();
                            match tab {
                                0 => view! { <DiffViewer state=state_signal /> }.into_any(),
                                1 => view! { <VerifierPanel state=state_signal /> }.into_any(),
                                2 => view! { <AuditTrail state=state_signal /> }.into_any(),
                                _ => view! { <div></div> }.into_any(),
                            }
                        }}
                    </Tabs>
                </div>
            </div>
        </div>
    }
}
