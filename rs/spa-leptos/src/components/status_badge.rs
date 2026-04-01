use leptos::prelude::*;

use crate::api::types::RunStatus;

#[component]
pub fn StatusBadge(status: RunStatus, #[prop(optional)] pending_approval: bool) -> impl IntoView {
    let base_style = "font-size:11px;padding:2px 10px;border-radius:4px;font-weight:600;letter-spacing:0.5px;display:inline-block;";

    if pending_approval {
        view! {
            <span style=format!(
                "{}background:var(--warn);color:var(--bg-void);",
                base_style,
            )>
                "AWAITING APPROVAL"
            </span>
        }
        .into_any()
    } else {
        match status {
            RunStatus::Running => view! {
                <span style=format!(
                    "{}background:linear-gradient(135deg,var(--accent),var(--accent-alt));color:var(--bg-void);",
                    base_style,
                )>
                    "RUNNING"
                </span>
            }
            .into_any(),
            RunStatus::Completed => view! {
                <span style=format!(
                    "{}background:color-mix(in srgb,var(--ok) 20%,transparent);color:var(--ok);border:1px solid color-mix(in srgb,var(--ok) 30%,transparent);",
                    base_style,
                )>
                    "PASSED"
                </span>
            }
            .into_any(),
            RunStatus::Failed => view! {
                <span style=format!(
                    "{}background:color-mix(in srgb,var(--err) 20%,transparent);color:var(--err);border:1px solid color-mix(in srgb,var(--err) 30%,transparent);",
                    base_style,
                )>
                    "FAILED"
                </span>
            }
            .into_any(),
            RunStatus::Suspended => view! {
                <span style=format!(
                    "{}background:color-mix(in srgb,#a78bfa 20%,transparent);color:#a78bfa;border:1px solid color-mix(in srgb,#a78bfa 30%,transparent);",
                    base_style,
                )>
                    "SUSPENDED"
                </span>
            }
            .into_any(),
            RunStatus::Queued => view! {
                <span style=format!(
                    "{}background:color-mix(in srgb,var(--text-muted) 15%,transparent);color:var(--text-muted);border:1px solid var(--border);",
                    base_style,
                )>
                    "QUEUED"
                </span>
            }
            .into_any(),
        }
    }
}
