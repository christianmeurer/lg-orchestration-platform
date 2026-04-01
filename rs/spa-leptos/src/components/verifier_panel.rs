use leptos::prelude::*;

use crate::api::{sse::RunState, types::VerifierReport};

#[component]
pub fn VerifierPanel(#[prop(into)] state: Signal<RunState>) -> impl IntoView {
    let report = move || {
        let s = state.get();
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
                match report() {
                    None => view! {
                        <div style="color:var(--text-muted);font-size:13px;padding:20px;text-align:center;">
                            "Verification not yet run."
                        </div>
                    }
                    .into_any(),
                    Some(r) => {
                        let overall_ok = r.ok;
                        let (icon, label, color) = if overall_ok {
                            ("\u{2713}", "PASSED", "var(--ok)")
                        } else {
                            ("\u{2717}", "FAILED", "var(--err)")
                        };
                        let checks = r.checks;
                        let halt_reason = r.halt_reason;
                        view! {
                            <div style="display:flex;flex-direction:column;gap:12px;">
                                <div style=format!(
                                    "display:flex;align-items:center;gap:8px;font-size:16px;font-weight:600;color:{};",
                                    color,
                                )>
                                    <span>{icon}</span>
                                    <span>{label}</span>
                                </div>
                                <div style="display:flex;flex-direction:column;gap:4px;">
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
                                                <div style="display:flex;align-items:center;gap:8px;padding:4px 0;">
                                                    <span style=format!("color:{};font-size:14px;", cc)>
                                                        {ci}
                                                    </span>
                                                    <span style="font-size:13px;color:var(--text-primary);">
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
                                {halt_reason.map(|reason| {
                                    view! {
                                        <div style="border:1px solid var(--err);border-radius:6px;padding:10px 14px;background:color-mix(in srgb,var(--err) 10%,transparent);">
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
