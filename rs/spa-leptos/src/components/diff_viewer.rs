use leptos::prelude::*;

use crate::api::sse::RunState;

#[component]
pub fn DiffViewer(#[prop(into)] state: Signal<RunState>) -> impl IntoView {
    let diffs = move || {
        let s = state.get();
        s.events
            .iter()
            .filter(|ev| ev.kind.as_deref() == Some("tool_result"))
            .filter_map(|ev| {
                let data = ev.data.as_ref()?;
                let tool = data.get("tool")?.as_str()?;
                if tool == "apply_patch" || tool == "write_file" {
                    data.get("stdout")?.as_str().map(|s| s.to_string())
                } else {
                    None
                }
            })
            .collect::<Vec<String>>()
    };

    view! {
        <div style="display:flex;flex-direction:column;gap:12px;">
            {move || {
                let diff_blocks = diffs();
                if diff_blocks.is_empty() {
                    view! {
                        <div style="color:var(--text-muted);font-size:13px;padding:20px;text-align:center;">
                            "No file changes detected yet."
                        </div>
                    }
                    .into_any()
                } else {
                    view! {
                        <div>
                            {diff_blocks
                                .into_iter()
                                .map(|block| {
                                    view! {
                                        <DiffBlock content=block />
                                    }
                                })
                                .collect::<Vec<_>>()}
                        </div>
                    }
                    .into_any()
                }
            }}
        </div>
    }
}

fn line_color(line: &str) -> &'static str {
    if line.starts_with('+') {
        "var(--ok)"
    } else if line.starts_with('-') {
        "var(--err)"
    } else if line.starts_with("@@") {
        "var(--info)"
    } else {
        "var(--text-muted)"
    }
}

#[component]
fn DiffBlock(content: String) -> impl IntoView {
    let lines: Vec<String> = content.lines().map(|l| l.to_string()).collect();

    view! {
        <div style="background:var(--bg-void);border:1px solid var(--border);border-radius:6px;padding:10px 12px;font-family:monospace;font-size:12px;overflow-x:auto;">
            {lines
                .into_iter()
                .map(|line| {
                    let color = line_color(&line);
                    view! {
                        <div style=format!("color:{};padding:1px 0;white-space:pre;", color)>
                            {line}
                        </div>
                    }
                })
                .collect::<Vec<_>>()}
        </div>
    }
}
