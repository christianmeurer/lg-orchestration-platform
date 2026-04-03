use leptos::prelude::*;

use crate::api::{sse::RunState, types::VerifierReport};

#[component]
pub fn VerifierPanel(#[prop(into)] state: Signal<RunState>) -> impl IntoView {
    let report = move || {
        let s = state.get();

        // 1. Try extracting from trace_json (available when trace_ready)
        if let Some(ref trace) = s.trace_json {
            if let Some(verification) = trace.get("verification") {
                if let Ok(r) = serde_json::from_value::<VerifierReport>(verification.clone()) {
                    return Some(r);
                }
            }
        }

        // 2. Fall back to structured trace events
        s.events
            .iter()
            .rev()
            .find(|ev| {
                let kind = ev.kind.as_deref();
                kind == Some("verifier_pass") || kind == Some("verifier_fail")
            })
            .and_then(|ev| {
                ev.data
                    .as_ref()
                    .and_then(|d| serde_json::from_value::<VerifierReport>(d.clone()).ok())
            })
    };

    view! {
        <div>
            {move || {
                let s = state.get();
                match report() {
                    None => {
                        let msg = if s.is_done {
                            "No verification data available."
                        } else {
                            "Verification runs after completion."
                        };
                        view! {
                            <div class="empty-state" style="padding:40px 20px;">
                                <div style="font-size:28px;opacity:0.2;margin-bottom:12px;">"\u{1F6E1}"</div>
                                <div style="font-size:13px;color:var(--text-muted);">{msg}</div>
                            </div>
                        }
                        .into_any()
                    }
                    Some(r) => {
                        let overall_ok = r.ok;
                        let acceptance_ok = r.acceptance_ok;
                        let (icon, label, color) = if overall_ok {
                            ("\u{2713}", "PASSED", "var(--ok)")
                        } else {
                            ("\u{2717}", "FAILED", "var(--err)")
                        };
                        let checks = r.checks;
                        let halt_reason = r.halt_reason;
                        view! {
                            <div style="display:flex;flex-direction:column;gap:16px;">
                                // Overall status
                                <div style=format!(
                                    "display:flex;align-items:center;gap:10px;font-size:18px;font-weight:600;color:{};padding:12px 16px;background:color-mix(in srgb, {} 8%, transparent);border-radius:var(--radius-md);border:1px solid color-mix(in srgb, {} 20%, transparent);",
                                    color, color, color,
                                )>
                                    <span style="font-size:22px;">{icon}</span>
                                    <span>{label}</span>
                                </div>

                                // Acceptance criteria
                                {acceptance_ok.map(|acc| {
                                    let (acc_icon, acc_color, acc_label) = if acc {
                                        ("\u{2713}", "var(--ok)", "Acceptance criteria met")
                                    } else {
                                        ("\u{2717}", "var(--err)", "Acceptance criteria NOT met")
                                    };
                                    view! {
                                        <div style=format!(
                                            "display:flex;align-items:center;gap:8px;padding:8px 12px;font-size:13px;color:{};",
                                            acc_color,
                                        )>
                                            <span>{acc_icon}</span>
                                            <span>{acc_label}</span>
                                        </div>
                                    }
                                })}

                                // Individual checks
                                <div style="display:flex;flex-direction:column;gap:2px;">
                                    <div style="font-size:11px;color:var(--text-muted);letter-spacing:0.5px;font-weight:600;margin-bottom:6px;">
                                        "CHECKS"
                                    </div>
                                    {checks
                                        .into_iter()
                                        .map(|check| {
                                            let (ci, cc) = if check.ok {
                                                ("\u{2713}", "var(--ok)")
                                            } else {
                                                ("\u{2717}", "var(--err)")
                                            };
                                            let name = check.name;
                                            let summary = check.summary.unwrap_or_default();
                                            view! {
                                                <div style="display:flex;align-items:center;gap:8px;padding:6px 12px;border-radius:var(--radius-sm);background:var(--bg-elevated);">
                                                    <span style=format!("color:{};font-size:14px;flex-shrink:0;", cc)>
                                                        {ci}
                                                    </span>
                                                    <span style="font-size:13px;color:var(--text-primary);font-weight:500;">
                                                        {name}
                                                    </span>
                                                    {if !summary.is_empty() {
                                                        Some(view! {
                                                            <span style="font-size:12px;color:var(--text-muted);margin-left:auto;">
                                                                {summary}
                                                            </span>
                                                        })
                                                    } else {
                                                        None
                                                    }}
                                                </div>
                                            }
                                        })
                                        .collect::<Vec<_>>()}
                                </div>

                                // Halt reason
                                {halt_reason.map(|reason| {
                                    view! {
                                        <div style="border:1px solid var(--err);border-radius:var(--radius-md);padding:12px 16px;background:color-mix(in srgb,var(--err) 8%,transparent);">
                                            <div style="font-size:11px;color:var(--err);margin-bottom:4px;font-weight:600;letter-spacing:0.5px;">
                                                "HALT REASON"
                                            </div>
                                            <div style="font-size:13px;color:var(--text-primary);">
                                                {reason}
                                            </div>
                                        </div>
                                    }
                                })}
                            </div>
                        }
                        .into_any()
                    }
                }
            }}
        </div>
    }
}
