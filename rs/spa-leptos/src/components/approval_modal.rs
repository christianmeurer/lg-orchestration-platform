use leptos::prelude::*;

use crate::api::types::ApprovalRequest;

#[component]
pub fn ApprovalModal(
    request: ApprovalRequest,
    on_approve: Callback<()>,
    on_reject: Callback<()>,
) -> impl IntoView {
    let (is_acting, set_is_acting) = signal(false);

    let summary_text = request
        .summary
        .clone()
        .unwrap_or_else(|| "An operation requires your approval.".to_string());
    let operation_class = request.operation_class.clone().unwrap_or_else(|| "unknown".to_string());
    let challenge_display = request
        .challenge_id
        .as_ref()
        .map(|id| if id.len() > 12 { format!("{}...", &id[..12]) } else { id.clone() })
        .unwrap_or_else(|| "--".to_string());

    view! {
        <div
            class="modal-backdrop"
            style="position:fixed;inset:0;background:rgba(0,0,0,0.7);display:flex;align-items:center;justify-content:center;z-index:1000;"
        >
            <div
                class="modal-content"
                style="background:var(--bg-surface);border:1px solid var(--warn);border-radius:12px;padding:28px 32px;max-width:480px;width:100%;"
            >
                <div style="display:flex;align-items:center;gap:8px;margin-bottom:20px;">
                    <span style="font-size:20px;">
                        "\u{26A0}"
                    </span>
                    <h2 style="margin:0;font-size:18px;font-weight:600;color:var(--text-primary);">
                        "Approval Required"
                    </h2>
                </div>
                <p style="color:var(--text-secondary);font-size:14px;line-height:1.5;margin-bottom:20px;">
                    {summary_text}
                </p>
                <div style="background:var(--bg-void);border:1px solid var(--border);border-radius:6px;padding:12px 16px;margin-bottom:24px;">
                    <div style="display:flex;justify-content:space-between;margin-bottom:6px;">
                        <span style="font-size:12px;color:var(--text-muted);">"Operation"</span>
                        <span style="font-size:12px;color:var(--text-primary);font-weight:500;">
                            {operation_class}
                        </span>
                    </div>
                    <div style="display:flex;justify-content:space-between;">
                        <span style="font-size:12px;color:var(--text-muted);">"Challenge ID"</span>
                        <span style="font-size:12px;color:var(--text-primary);font-family:monospace;">
                            {challenge_display}
                        </span>
                    </div>
                </div>
                <div style="display:flex;gap:12px;justify-content:flex-end;">
                    <button
                        on:click=move |_| {
                            if !is_acting.get() {
                                set_is_acting.set(true);
                                on_reject.run(());
                            }
                        }
                        disabled=move || is_acting.get()
                        style="background:transparent;color:var(--text-secondary);border:1px solid var(--border);border-radius:6px;padding:8px 20px;font-size:13px;cursor:pointer;"
                    >
                        "Reject"
                    </button>
                    <button
                        on:click=move |_| {
                            if !is_acting.get() {
                                set_is_acting.set(true);
                                on_approve.run(());
                            }
                        }
                        disabled=move || is_acting.get()
                        style="background:linear-gradient(135deg,var(--accent),var(--accent-alt));color:var(--bg-void);border:none;border-radius:6px;padding:8px 20px;font-weight:600;font-size:13px;cursor:pointer;"
                    >
                        "Approve & Continue"
                    </button>
                </div>
            </div>
        </div>
    }
}
