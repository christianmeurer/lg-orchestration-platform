use leptos::prelude::*;
use leptos_router::components::Outlet;

use crate::api::client::{approve_run, reject_run, submit_run, ApiConfig};
use crate::api::types::ApprovalRequest;
use crate::components::approval_modal::ApprovalModal;
use crate::components::command_bar::CommandBar;

#[component]
pub fn ConsoleLayout() -> impl IntoView {
    let config = ApiConfig::new();
    provide_context(config.clone());

    let approval_signal: RwSignal<Option<ApprovalRequest>> = RwSignal::new(None);
    provide_context(approval_signal);

    let approval_count: RwSignal<usize> = RwSignal::new(0usize);
    provide_context(approval_count);

    let on_submit = {
        let config = config.clone();
        Callback::new(move |request: String| {
            let config = config.clone();
            leptos::task::spawn_local(async move {
                match submit_run(&config, &request).await {
                    Ok(summary) => {
                        let href = format!("/app/runs/{}", summary.run_id);
                        let _ = web_sys::window()
                            .unwrap()
                            .location()
                            .set_href(&href);
                    }
                    Err(e) => {
                        web_sys::console::error_1(
                            &format!("submit_run failed: {}", e).into(),
                        );
                    }
                }
            });
        })
    };

    let approval_count_signal: Signal<usize> = Signal::derive(move || approval_count.get());

    let on_approve = {
        let config = config.clone();
        Callback::new(move |_: ()| {
            let config = config.clone();
            let req = approval_signal.get_untracked();
            leptos::task::spawn_local(async move {
                if let Some(req) = req {
                    let _ = approve_run(&config, &req.run_id, req.challenge_id).await;
                }
                approval_signal.set(None);
            });
        })
    };

    let on_reject = {
        let config = config.clone();
        Callback::new(move |_: ()| {
            let config = config.clone();
            let req = approval_signal.get_untracked();
            leptos::task::spawn_local(async move {
                if let Some(req) = req {
                    let _ = reject_run(&config, &req.run_id).await;
                }
                approval_signal.set(None);
            });
        })
    };

    view! {
        <div style="display:flex;flex-direction:column;min-height:100vh;background:var(--bg-void);color:var(--text-primary);font-family:Inter,sans-serif;">
            <CommandBar on_submit=on_submit approval_count=approval_count_signal />
            <div style="flex:1;overflow:auto;">
                <Outlet />
            </div>
            {move || {
                approval_signal.get().map(|req| {
                    view! {
                        <ApprovalModal
                            request=req
                            on_approve=on_approve.clone()
                            on_reject=on_reject.clone()
                        />
                    }
                })
            }}
        </div>
    }
}
