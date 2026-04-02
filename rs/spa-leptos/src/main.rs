mod api;
mod app;
mod components;
mod layouts;
mod pages;

use app::App;

fn main() {
    console_error_panic_hook::set_once();

    // Apply saved theme on startup
    if let Some(window) = web_sys::window() {
        let theme = window
            .local_storage()
            .ok()
            .flatten()
            .and_then(|s| s.get_item("lula_theme").ok().flatten())
            .unwrap_or_else(|| "dark".to_string());
        if let Some(document) = window.document() {
            if let Some(el) = document.document_element() {
                let _ = el.set_attribute("data-theme", &theme);
            }
        }
    }

    leptos::mount::mount_to_body(App);
}
