use leptos::prelude::*;

#[component]
pub fn Tabs(tabs: Vec<String>, active: RwSignal<usize>, children: Children) -> impl IntoView {
    view! {
        <div>
            <div style="display:flex;gap:0;border-bottom:1px solid var(--border);margin-bottom:16px;">
                {tabs
                    .into_iter()
                    .enumerate()
                    .map(|(i, label)| {
                        let label_clone = label.clone();
                        view! {
                            <button
                                on:click=move |_| active.set(i)
                                style:background=move || {
                                    if active.get() == i {
                                        "var(--bg-elevated)"
                                    } else {
                                        "transparent"
                                    }
                                }
                                style:color=move || {
                                    if active.get() == i {
                                        "var(--text-primary)"
                                    } else {
                                        "var(--text-secondary)"
                                    }
                                }
                                style:border-bottom=move || {
                                    if active.get() == i {
                                        "2px solid var(--accent)"
                                    } else {
                                        "2px solid transparent"
                                    }
                                }
                                style="padding:8px 20px;border:none;cursor:pointer;font-size:13px;font-weight:500;transition:all 0.15s;"
                            >
                                {label_clone}
                            </button>
                        }
                    })
                    .collect::<Vec<_>>()}
            </div>
            <div>
                {children()}
            </div>
        </div>
    }
}
