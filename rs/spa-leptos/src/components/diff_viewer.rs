use leptos::prelude::*;

use crate::api::{sse::RunState, types::ToolEnvelope};

#[component]
pub fn DiffViewer(#[prop(into)] state: Signal<RunState>) -> impl IntoView {
    let diffs = move || {
        let s = state.get();
        let mut blocks: Vec<String> = Vec::new();

        // 1. From trace_json tool_results (when trace is available)
        if let Some(ref trace) = s.trace_json {
            if let Some(tool_results) = trace.get("tool_results") {
                if let Some(arr) = tool_results.as_array() {
                    for val in arr {
                        if let Ok(env) = serde_json::from_value::<ToolEnvelope>(val.clone()) {
                            if env.tool == "apply_patch" || env.tool == "write_file" {
                                if let Some(stdout) = env.stdout {
                                    if !stdout.is_empty() {
                                        blocks.push(stdout);
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }

        // 2. From structured trace events
        if blocks.is_empty() {
            for ev in &s.events {
                if ev.kind.as_deref() == Some("tool_result") {
                    if let Some(ref data) = ev.data {
                        let tool = data.get("tool").and_then(|v| v.as_str()).unwrap_or("");
                        if tool == "apply_patch" || tool == "write_file" {
                            if let Some(stdout) = data.get("stdout").and_then(|v| v.as_str()) {
                                if !stdout.is_empty() {
                                    blocks.push(stdout.to_string());
                                }
                            }
                        }
                    }
                }
            }
        }

        // 3. From log lines — extract lines that look like diffs
        if blocks.is_empty() {
            let mut current_diff = String::new();
            for line in &s.log_lines {
                let trimmed = line.trim();
                if trimmed.starts_with("--- ")
                    || trimmed.starts_with("+++ ")
                    || trimmed.starts_with("@@ ")
                    || trimmed.starts_with('+')
                    || trimmed.starts_with('-')
                    || trimmed.starts_with("diff ")
                {
                    if !current_diff.is_empty() {
                        current_diff.push('\n');
                    }
                    current_diff.push_str(trimmed);
                } else if !current_diff.is_empty() {
                    // End of diff block
                    if current_diff.lines().count() >= 3 {
                        blocks.push(current_diff.clone());
                    }
                    current_diff.clear();
                }
            }
            if !current_diff.is_empty() && current_diff.lines().count() >= 3 {
                blocks.push(current_diff);
            }
        }

        blocks
    };

    view! {
        <div style="display:flex;flex-direction:column;gap:12px;">
            {move || {
                let s = state.get();
                let diff_blocks = diffs();
                if diff_blocks.is_empty() {
                    let msg = if s.is_done {
                        "No file changes detected."
                    } else {
                        "No file changes detected yet."
                    };
                    view! {
                        <div class="empty-state" style="padding:40px 20px;">
                            <div style="font-size:28px;opacity:0.2;margin-bottom:12px;">"\u{1F4DD}"</div>
                            <div style="font-size:13px;color:var(--text-muted);">{msg}</div>
                        </div>
                    }
                    .into_any()
                } else {
                    view! {
                        <div style="display:flex;flex-direction:column;gap:8px;">
                            {diff_blocks
                                .into_iter()
                                .map(|block| {
                                    view! { <DiffBlock content=block /> }
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

fn line_bg(line: &str) -> &'static str {
    if line.starts_with('+') {
        "color-mix(in srgb, var(--ok) 6%, transparent)"
    } else if line.starts_with('-') {
        "color-mix(in srgb, var(--err) 6%, transparent)"
    } else {
        "transparent"
    }
}

#[component]
fn DiffBlock(content: String) -> impl IntoView {
    let lines: Vec<String> = content.lines().map(|l| l.to_string()).collect();

    view! {
        <div style="background:var(--bg-void);border:1px solid var(--border);border-radius:var(--radius-md);overflow:hidden;font-family:var(--font-mono);font-size:12px;">
            {lines
                .into_iter()
                .enumerate()
                .map(|(i, line)| {
                    let color = line_color(&line);
                    let bg = line_bg(&line);
                    let line_num = format!("{}", i + 1);
                    view! {
                        <div style=format!(
                            "display:flex;background:{};",
                            bg,
                        )>
                            <span style="width:40px;text-align:right;padding:1px 8px 1px 0;color:var(--text-faint);user-select:none;flex-shrink:0;border-right:1px solid var(--border);">
                                {line_num}
                            </span>
                            <span style=format!(
                                "color:{};padding:1px 8px;white-space:pre;overflow-x:auto;flex:1;",
                                color,
                            )>
                                {line}
                            </span>
                        </div>
                    }
                })
                .collect::<Vec<_>>()}
        </div>
    }
}
