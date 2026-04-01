mod api;
mod app;
mod components;
mod layouts;
mod pages;

use app::App;

fn main() {
    console_error_panic_hook::set_once();
    leptos::mount::mount_to_body(App);
}
