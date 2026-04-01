use leptos::prelude::*;

use crate::api::client::{fetch_runs, ApiConfig};
use crate::api::types::RunSummary;
use crate::components::metrics_cards::MetricsCards;
use crate::components::run_card::RunCard;

#[component]
pub fn DashboardPage() -> impl IntoView {
    let config = use_context::<ApiConfig>().unwrap();
    let runs: RwSignal<Vec<RunSummary>> = RwSignal::new(Vec::new());
    let error: RwSignal<Option<String>> = RwSignal::new(None);

    // Update the approval count context whenever runs change
    let approval_count = use_context::<RwSignal<usize>>();

    {
        let config = config.clone();
        leptos::task::spawn_local(async move {
            loop {
                match fetch_runs(&config).await {
                    Ok(fetched) => {
                        if let Some(ac) = approval_count {
                            let pending = fetched.iter().filter(|r| r.pending_approval).count();
                            ac.set(pending);
                        }
                        runs.set(fetched);
                        error.set(None);
                    }
                    Err(e) => {
                        error.set(Some(e));
                    }
                }
                gloo_timers::future::TimeoutFuture::new(5_000).await;
            }
        });
    }

    let runs_signal: Signal<Vec<RunSummary>> = Signal::derive(move || runs.get());

    view! {
        <div style="display:flex;height:100%;">
            // Left panel: runs list
            <div style="width:380px;flex-shrink:0;border-right:1px solid var(--border);display:flex;flex-direction:column;overflow:hidden;">
                <div style="padding:16px;font-size:11px;color:var(--text-muted);letter-spacing:0.5px;font-weight:600;">
                    "RUNS"
                </div>
                {move || {
                    error.get().map(|e| {
                        view! {
                            <div style="padding:8px 16px;font-size:12px;color:var(--err);">
                                {e}
                            </div>
                        }
                    })
                }}
                <div style="flex:1;overflow-y:auto;padding:0 12px 12px 12px;display:flex;flex-direction:column;gap:8px;">
                    {move || {
                        runs.get()
                            .into_iter()
                            .map(|run| {
                                let run_id = run.run_id.clone();
                                view! {
                                    <a href=format!("/app/runs/{}", run_id) style="text-decoration:none;">
                                        <RunCard run=run />
                                    </a>
                                }
                            })
                            .collect::<Vec<_>>()
                    }}
                </div>
            </div>
            // Right panel: overview
            <div style="flex:1;padding:24px;overflow-y:auto;">
                <div style="font-size:11px;color:var(--text-muted);letter-spacing:0.5px;font-weight:600;margin-bottom:16px;">
                    "OVERVIEW"
                </div>
                <MetricsCards runs=runs_signal />
            </div>
        </div>
    }
}
