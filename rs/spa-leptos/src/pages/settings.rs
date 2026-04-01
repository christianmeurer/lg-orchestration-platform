use leptos::prelude::*;
use wasm_bindgen::JsCast;
use web_sys::HtmlInputElement;

use crate::api::client::ApiConfig;

#[component]
pub fn SettingsPage() -> impl IntoView {
    let config = use_context::<ApiConfig>().unwrap();
    let server_url = config.base_url.clone();
    let (token_value, set_token_value) = signal(config.token.get_untracked().unwrap_or_default());
    let (saved, set_saved) = signal(false);

    let on_save = {
        let config = config.clone();
        move |_| {
            let new_token = token_value.get();
            if new_token.is_empty() {
                config.token.set(None);
            } else {
                config.token.set(Some(new_token));
            }
            config.save();
            set_saved.set(true);
            let set_saved = set_saved;
            gloo_timers::callback::Timeout::new(2_000, move || {
                set_saved.set(false);
            })
            .forget();
        }
    };

    view! {
        <div style="padding:24px;max-width:520px;">
            <div style="font-size:11px;color:var(--text-muted);letter-spacing:0.5px;font-weight:600;margin-bottom:24px;">
                "SETTINGS"
            </div>

            // Server URL
            <div style="margin-bottom:20px;">
                <label style="display:block;font-size:12px;color:var(--text-secondary);margin-bottom:6px;font-weight:500;">
                    "Server URL"
                </label>
                <input
                    type="text"
                    disabled=true
                    prop:value=server_url
                    style="width:100%;background:var(--bg-void);color:var(--text-muted);border:1px solid var(--border);border-radius:6px;padding:8px 12px;font-size:13px;box-sizing:border-box;"
                />
                <div style="font-size:11px;color:var(--text-faint);margin-top:4px;">
                    "Set via LG_SPA_API_URL environment variable"
                </div>
            </div>

            // Token
            <div style="margin-bottom:24px;">
                <label style="display:block;font-size:12px;color:var(--text-secondary);margin-bottom:6px;font-weight:500;">
                    "Token"
                </label>
                <input
                    type="password"
                    prop:value=move || token_value.get()
                    on:input=move |ev| {
                        let target = ev.target().unwrap().unchecked_into::<HtmlInputElement>();
                        set_token_value.set(target.value());
                    }
                    style="width:100%;background:var(--bg-void);color:var(--text-primary);border:1px solid var(--border);border-radius:6px;padding:8px 12px;font-size:13px;box-sizing:border-box;"
                />
            </div>

            // Save button
            <div style="display:flex;align-items:center;gap:12px;">
                <button
                    on:click=on_save
                    style="background:linear-gradient(135deg,var(--accent),var(--accent-alt));color:var(--bg-void);border:none;border-radius:6px;padding:8px 24px;font-weight:600;font-size:13px;cursor:pointer;"
                >
                    "Save"
                </button>
                {move || {
                    if saved.get() {
                        Some(view! {
                            <span style="font-size:13px;color:var(--ok);">
                                "Saved \u{2713}"
                            </span>
                        })
                    } else {
                        None
                    }
                }}
            </div>
        </div>
    }
}
