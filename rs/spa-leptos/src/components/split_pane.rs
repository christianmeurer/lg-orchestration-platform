use leptos::prelude::*;
use wasm_bindgen::{closure::Closure, JsCast};

/// A resizable split-pane container. Renders a left and right panel separated
/// by a draggable divider. The left panel width is persisted to `localStorage`
/// under `lula_split_pane_width`.
#[component]
pub fn SplitPane(
    /// Initial width of the left panel as a percentage (e.g. 35.0).
    #[prop(default = 35.0)]
    initial_left_width: f64,
    /// Content for the left panel.
    left: ViewFn,
    /// Content for the right panel.
    right: ViewFn,
) -> impl IntoView {
    // Restore persisted width or fall back to initial.
    let stored = web_sys::window()
        .and_then(|w| w.local_storage().ok().flatten())
        .and_then(|s| s.get_item("lula_split_pane_width").ok().flatten())
        .and_then(|v| v.parse::<f64>().ok());

    let (left_pct, set_left_pct) = signal(stored.unwrap_or(initial_left_width));
    let (dragging, set_dragging) = signal(false);

    // Attach document-level mousemove / mouseup while dragging.
    Effect::new(move |_| {
        let window = web_sys::window().unwrap();
        let document = window.document().unwrap();

        let on_move =
            Closure::<dyn Fn(web_sys::MouseEvent)>::new(move |ev: web_sys::MouseEvent| {
                if !dragging.get_untracked() {
                    return;
                }
                let doc = web_sys::window().unwrap().document().unwrap();
                let body_width = doc.body().map(|b| b.client_width()).unwrap_or(1) as f64;
                if body_width < 1.0 {
                    return;
                }
                let pct = (ev.client_x() as f64 / body_width * 100.0).clamp(15.0, 85.0);
                set_left_pct.set(pct);
            });

        let on_up = Closure::<dyn Fn(web_sys::MouseEvent)>::new(move |_: web_sys::MouseEvent| {
            if !dragging.get_untracked() {
                return;
            }
            set_dragging.set(false);
            // Persist
            if let Some(storage) = web_sys::window().and_then(|w| w.local_storage().ok().flatten())
            {
                let _ = storage
                    .set_item("lula_split_pane_width", &format!("{:.1}", left_pct.get_untracked()));
            }
        });

        document
            .add_event_listener_with_callback("mousemove", on_move.as_ref().unchecked_ref())
            .unwrap();
        document
            .add_event_listener_with_callback("mouseup", on_up.as_ref().unchecked_ref())
            .unwrap();
        on_move.forget();
        on_up.forget();
    });

    view! {
        <div style="display:flex;height:100%;position:relative;">
            <div style:width=move || format!("{}%", left_pct.get())
                 style="flex-shrink:0;overflow:hidden;display:flex;flex-direction:column;">
                {left.run()}
            </div>
            <div
                on:mousedown=move |ev: web_sys::MouseEvent| {
                    ev.prevent_default();
                    set_dragging.set(true);
                }
                style:cursor="col-resize"
                style="width:4px;flex-shrink:0;background:var(--border);transition:background 0.15s;"
                style:background=move || if dragging.get() { "var(--accent)" } else { "var(--border)" }
                on:mouseenter=move |ev: web_sys::MouseEvent| {
                    if let Some(target) = ev.target() {
                        if let Some(el) = target.dyn_ref::<web_sys::HtmlElement>() {
                            let _ = el.style().set_property("background", "var(--accent)");
                        }
                    }
                }
                on:mouseleave=move |ev: web_sys::MouseEvent| {
                    if !dragging.get() {
                        if let Some(target) = ev.target() {
                            if let Some(el) = target.dyn_ref::<web_sys::HtmlElement>() {
                                let _ = el.style().set_property("background", "var(--border)");
                            }
                        }
                    }
                }
            />
            <div style="flex:1;overflow:hidden;display:flex;flex-direction:column;">
                {right.run()}
            </div>
        </div>
    }
}
