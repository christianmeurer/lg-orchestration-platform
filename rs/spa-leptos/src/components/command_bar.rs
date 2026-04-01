use leptos::prelude::*;
use wasm_bindgen::JsCast;
use web_sys::HtmlInputElement;

#[component]
pub fn CommandBar(
    on_submit: Callback<String>,
    #[prop(optional, into)] approval_count: Option<Signal<usize>>,
) -> impl IntoView {
    let (value, set_value) = signal(String::new());
    let (is_submitting, set_is_submitting) = signal(false);

    let do_submit = move || {
        let v = value.get();
        let trimmed = v.trim().to_string();
        if trimmed.is_empty() || is_submitting.get() {
            return;
        }
        set_is_submitting.set(true);
        on_submit.run(trimmed);
        set_value.set(String::new());
        // Reset submitting state after a brief delay
        gloo_timers::callback::Timeout::new(500, move || {
            set_is_submitting.set(false);
        })
        .forget();
    };

    let do_submit_click = do_submit;

    view! {
        <div style="display:flex;align-items:center;gap:12px;padding:12px 16px;background:var(--bg-surface);border-bottom:1px solid var(--border);">
            <span style="color:var(--text-muted);font-size:16px;flex-shrink:0;">
                "\u{2318}"
            </span>
            <input
                type="text"
                placeholder="Describe a task..."
                prop:value=move || value.get()
                on:input=move |ev| {
                    let target = ev.target().unwrap().unchecked_into::<HtmlInputElement>();
                    set_value.set(target.value());
                }
                on:keydown=move |ev: web_sys::KeyboardEvent| {
                    if ev.key() == "Enter" && !ev.shift_key() {
                        ev.prevent_default();
                        do_submit();
                    }
                }
                style="flex:1;background:var(--bg-void);color:var(--text-primary);border:1px solid var(--border);border-radius:6px;padding:8px 12px;font-size:14px;outline:none;"
            />
            {move || {
                approval_count.and_then(|sig| {
                    let count = sig.get();
                    if count > 0 {
                        Some(view! {
                            <a
                                href="/app/approvals"
                                style="background:var(--warn);color:var(--bg-void);padding:4px 10px;border-radius:4px;font-size:12px;font-weight:600;text-decoration:none;"
                            >
                                {format!("{} pending", count)}
                            </a>
                        })
                    } else {
                        None
                    }
                })
            }}
            <button
                on:click=move |_| do_submit_click()
                disabled=move || is_submitting.get()
                style="background:linear-gradient(135deg,var(--accent),var(--accent-alt));color:var(--bg-void);border:none;border-radius:6px;padding:8px 20px;font-weight:600;font-size:13px;cursor:pointer;flex-shrink:0;"
            >
                "Submit"
            </button>
        </div>
    }
}
