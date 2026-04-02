use leptos::prelude::*;
use leptos_router::components::Outlet;
use wasm_bindgen::{closure::Closure, JsCast};

use crate::{
    api::{
        client::{approve_run, reject_run, submit_run, ApiConfig},
        types::ApprovalRequest,
    },
    components::{approval_modal::ApprovalModal, command_bar::CommandBar},
};

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
                        let _ = web_sys::window().unwrap().location().set_href(&href);
                    }
                    Err(e) => {
                        web_sys::console::error_1(&format!("submit_run failed: {}", e).into());
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

    // Global keyboard shortcuts
    Effect::new(move |_| {
        let window = web_sys::window().unwrap();
        let document = window.document().unwrap();
        let closure =
            Closure::<dyn Fn(web_sys::KeyboardEvent)>::new(move |ev: web_sys::KeyboardEvent| {
                let key = ev.key();

                // Ctrl+Enter or Cmd+Enter → focus command bar and submit
                if key == "Enter" && (ev.ctrl_key() || ev.meta_key()) {
                    ev.prevent_default();
                    let doc = web_sys::window().unwrap().document().unwrap();
                    if let Some(input) =
                        doc.query_selector("input[placeholder='Describe a task...']").ok().flatten()
                    {
                        if let Some(el) = input.dyn_ref::<web_sys::HtmlElement>() {
                            let _ = el.focus();
                        }
                        // Dispatch an Enter keydown event to trigger submit
                        let init = web_sys::KeyboardEventInit::new();
                        init.set_key("Enter");
                        init.set_bubbles(true);
                        if let Ok(enter_ev) =
                            web_sys::KeyboardEvent::new_with_keyboard_event_init_dict(
                                "keydown", &init,
                            )
                        {
                            let _ = input.dispatch_event(&enter_ev);
                        }
                    }
                }

                // Escape → dismiss approval modal
                if key == "Escape" {
                    approval_signal.set(None);
                }
            });
        document
            .add_event_listener_with_callback("keydown", closure.as_ref().unchecked_ref())
            .unwrap();
        closure.forget();
    });

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
                            on_approve=on_approve
                            on_reject=on_reject
                        />
                    }
                })
            }}
        </div>
    }
}
