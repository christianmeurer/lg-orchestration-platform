use leptos::prelude::*;
use leptos_meta::provide_meta_context;
use leptos_router::{
    components::{ParentRoute, Route, Router, Routes},
    path,
};

use crate::{
    layouts::console::ConsoleLayout,
    pages::{
        approvals::ApprovalsPage, dashboard::DashboardPage, run_detail::RunDetailPage,
        settings::SettingsPage,
    },
};

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
