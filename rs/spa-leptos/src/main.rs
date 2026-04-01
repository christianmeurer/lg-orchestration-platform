use leptos::prelude::*;

fn main() {
    console_error_panic_hook::set_once();
    mount_to_body(App);
}

#[component]
fn App() -> impl IntoView {
    view! {
        <main style="background:#050508;color:#e2e8f0;min-height:100vh;display:flex;align-items:center;justify-content:center;font-family:Inter,sans-serif">
            <h1 style="font-weight:300;color:#00d4aa">"Lula Console"</h1>
        </main>
    }
}
