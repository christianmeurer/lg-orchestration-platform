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
        <div class="command-bar">
            <span style="color:var(--accent);font-size:18px;flex-shrink:0;font-weight:700;">
                "\u{25C6}"
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
            />
            {move || {
                approval_count.and_then(|sig| {
                    let count = sig.get();
                    if count > 0 {
                        Some(view! {
                            <a
                                href="/app/approvals"
                                class="badge badge-pending"
                                style="text-decoration:none;font-weight:600;"
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
                class="submit-btn"
                on:click=move |_| do_submit_click()
                disabled=move || is_submitting.get()
            >
                "Submit"
            </button>
        </div>
    }
}
