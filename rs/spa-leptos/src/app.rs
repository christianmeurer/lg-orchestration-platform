use leptos::prelude::*;
use leptos_meta::provide_meta_context;
use leptos_router::components::{ParentRoute, Route, Router, Routes};
use leptos_router::path;

use crate::layouts::console::ConsoleLayout;
use crate::pages::approvals::ApprovalsPage;
use crate::pages::dashboard::DashboardPage;
use crate::pages::run_detail::RunDetailPage;
use crate::pages::settings::SettingsPage;

#[component]
pub fn App() -> impl IntoView {
    provide_meta_context();
    view! {
        <Router base="/app">
            <Routes fallback=|| view! { <div style="color:var(--text-muted);padding:40px;text-align:center;font-size:18px;">"404 — Not Found"</div> }>
                <ParentRoute path=path!("/") view=ConsoleLayout>
                    <Route path=path!("/") view=DashboardPage />
                    <Route path=path!("/runs/:id") view=RunDetailPage />
                    <Route path=path!("/approvals") view=ApprovalsPage />
                    <Route path=path!("/settings") view=SettingsPage />
                </ParentRoute>
            </Routes>
        </Router>
    }
}
