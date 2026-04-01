use leptos::prelude::*;

use crate::api::types::{RunStatus, RunSummary};

#[component]
pub fn MetricsCards(#[prop(into)] runs: Signal<Vec<RunSummary>>) -> impl IntoView {
    let total = move || runs.get().len();

    let pass_rate = move || {
        let r = runs.get();
        let finished = r.iter().filter(|r| r.status.is_terminal()).count();
        if finished == 0 {
            return 0_u32;
        }
        let passed = r.iter().filter(|r| r.status == RunStatus::Completed).count();
        ((passed as f64 / finished as f64) * 100.0) as u32
    };

    let pending = move || runs.get().iter().filter(|r| r.pending_approval).count();

    let active = move || runs.get().iter().filter(|r| r.status == RunStatus::Running).count();

    let card_style = "background:var(--bg-surface);border:1px solid var(--border);border-radius:8px;padding:16px 20px;";

    view! {
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;">
            <div style=card_style>
                <div style="font-size:11px;color:var(--text-muted);letter-spacing:0.5px;margin-bottom:6px;">
                    "RUNS"
                </div>
                <div style="font-size:28px;font-weight:700;color:var(--text-primary);">
                    {total}
                </div>
            </div>
            <div style=card_style>
                <div style="font-size:11px;color:var(--text-muted);letter-spacing:0.5px;margin-bottom:6px;">
                    "PASS RATE"
                </div>
                <div style="font-size:28px;font-weight:700;color:var(--ok);">
                    {move || format!("{}%", pass_rate())}
                </div>
            </div>
            <div style=card_style>
                <div style="font-size:11px;color:var(--text-muted);letter-spacing:0.5px;margin-bottom:6px;">
                    "PENDING"
                </div>
                <div style="font-size:28px;font-weight:700;color:var(--warn);">
                    {pending}
                </div>
            </div>
            <div style=card_style>
                <div style="font-size:11px;color:var(--text-muted);letter-spacing:0.5px;margin-bottom:6px;">
                    "ACTIVE"
                </div>
                <div style="font-size:28px;font-weight:700;color:var(--accent);">
                    {active}
                </div>
            </div>
        </div>
    }
}
