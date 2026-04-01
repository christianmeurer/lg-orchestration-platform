use leptos::prelude::*;

use crate::api::client::{fetch_runs, ApiConfig};
use crate::api::types::RunSummary;

#[component]
pub fn ApprovalsPage() -> impl IntoView {
    let config = use_context::<ApiConfig>().unwrap();
    let pending_runs: RwSignal<Vec<RunSummary>> = RwSignal::new(Vec::new());

    {
        let config = config.clone();
        leptos::task::spawn_local(async move {
            match fetch_runs(&config).await {
                Ok(all_runs) => {
                    let filtered: Vec<RunSummary> = all_runs
                        .into_iter()
                        .filter(|r| r.pending_approval)
                        .collect();
                    pending_runs.set(filtered);
                }
                Err(e) => {
                    web_sys::console::error_1(
                        &format!("Failed to fetch runs: {}", e).into(),
                    );
                }
            }
        });
    }

    view! {
        <div style="padding:24px;">
            <div style="font-size:11px;color:var(--text-muted);letter-spacing:0.5px;font-weight:600;margin-bottom:16px;">
                "APPROVAL QUEUE"
            </div>
            {move || {
                let runs = pending_runs.get();
                if runs.is_empty() {
                    view! {
                        <div style="color:var(--text-muted);font-size:14px;text-align:center;padding:40px;">
                            "No pending approvals."
                        </div>
                    }.into_any()
                } else {
                    view! {
                        <div style="display:flex;flex-direction:column;gap:10px;">
                            {runs.into_iter().map(|run| {
                                let href = format!("/app/runs/{}", run.run_id);
                                let request_summary = if run.request.len() > 60 {
                                    format!("{}...", &run.request[..60])
                                } else {
                                    run.request.clone()
                                };
                                let display_id = run.run_id.clone();
                                view! {
                                    <a
                                        href=href
                                        style="text-decoration:none;"
                                    >
                                        <div style="background:var(--bg-surface);border:1px solid var(--warn);border-radius:8px;padding:16px 20px;cursor:pointer;transition:border-color 0.15s;">
                                            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
                                                <span style="font-size:14px;color:var(--text-primary);font-weight:500;">
                                                    {request_summary}
                                                </span>
                                                <span style="font-size:12px;color:var(--warn);font-weight:600;">
                                                    "\u{26A0} Review"
                                                </span>
                                            </div>
                                            <div style="font-size:12px;color:var(--text-muted);font-family:monospace;">
                                                {display_id}
                                            </div>
                                        </div>
                                    </a>
                                }
                            }).collect::<Vec<_>>()}
                        </div>
                    }.into_any()
                }
            }}
        </div>
    }
}
