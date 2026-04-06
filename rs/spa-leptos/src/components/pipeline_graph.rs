use leptos::prelude::*;

use crate::api::sse::{PipelineNode, RunState};

/// Radius of each node circle in the SVG.
const R: f64 = 20.0;
/// Horizontal spacing between node centers.
const SPACING: f64 = 100.0;
/// Vertical center of the circles.
const CY: f64 = 40.0;
/// SVG height.
const SVG_H: f64 = 100.0;

fn node_color(node: &PipelineNode, is_last: bool, is_done: bool) -> &'static str {
    if is_done && node.done {
        "var(--ok)"
    } else if is_last && !is_done {
        "var(--accent)"
    } else if node.done {
        "var(--ok)"
    } else {
        "var(--border)"
    }
}

fn node_text_color(node: &PipelineNode, is_last: bool, is_done: bool) -> &'static str {
    if is_done && node.done {
        "var(--ok)"
    } else if is_last && !is_done {
        "var(--accent)"
    } else if node.done {
        "var(--ok)"
    } else {
        "var(--text-muted)"
    }
}

#[component]
pub fn PipelineGraph(#[prop(into)] state: Signal<RunState>) -> impl IntoView {
    view! {
        <div class="pipeline-svg-container">
            {move || {
                let s = state.get();
                let nodes = &s.pipeline_nodes;
                let is_done = s.is_done;

                if nodes.is_empty() {
                    return view! {
                        <div class="empty-state" style="padding:40px 20px;">
                            <div style="font-size:13px;color:var(--text-muted);">"Waiting for pipeline data..."</div>
                        </div>
                    }
                    .into_any();
                }

                let count = nodes.len();
                let svg_w = (count as f64 - 1.0) * SPACING + R * 4.0;
                let view_box = format!("0 0 {} {}", svg_w, SVG_H);

                // Build connector lines and node circles
                let mut lines_html = Vec::new();
                let mut circles_html = Vec::new();

                for (i, node) in nodes.iter().enumerate() {
                    let cx = R * 2.0 + (i as f64) * SPACING;
                    let is_last = i + 1 == count;
                    let fill = node_color(node, is_last, is_done);
                    let text_col = node_text_color(node, is_last, is_done);
                    let is_active = is_last && !is_done;

                    // Connector line to the next node
                    if i + 1 < count {
                        let x2 = cx + SPACING;
                        let line_color = if node.done { "var(--ok)" } else { "var(--border)" };
                        lines_html.push(view! {
                            <line
                                x1=format!("{}", cx + R)
                                y1=format!("{}", CY)
                                x2=format!("{}", x2 - R)
                                y2=format!("{}", CY)
                                stroke=line_color
                                stroke-width="2"
                            />
                        });
                    }

                    let circle_class = if is_active {
                        "pipeline-node-active"
                    } else {
                        ""
                    };

                    // Checkmark for done nodes
                    let check_icon = if node.done {
                        Some(view! {
                            <text
                                x=format!("{}", cx)
                                y=format!("{}", CY + 5.0)
                                text-anchor="middle"
                                fill="var(--bg-void)"
                                font-size="16"
                                font-weight="bold"
                            >
                                "\u{2713}"
                            </text>
                        })
                    } else {
                        None
                    };

                    // Tool count badge
                    let tool_badge = if node.tools > 0 {
                        Some(view! {
                            <g>
                                <circle
                                    cx=format!("{}", cx + R * 0.7)
                                    cy=format!("{}", CY - R * 0.7)
                                    r="8"
                                    fill="var(--accent-blue)"
                                />
                                <text
                                    x=format!("{}", cx + R * 0.7)
                                    y=format!("{}", CY - R * 0.7 + 3.5)
                                    text-anchor="middle"
                                    fill="white"
                                    font-size="9"
                                    font-weight="600"
                                >
                                    {format!("{}", node.tools)}
                                </text>
                            </g>
                        })
                    } else {
                        None
                    };

                    let node_name = node.name.clone();
                    let cx_str = format!("{}", cx);
                    let cy_str = format!("{}", CY);
                    let r_str = format!("{}", R);
                    let label_y = format!("{}", CY + R + 16.0);

                    circles_html.push(view! {
                        <g class=circle_class>
                            <circle
                                cx=cx_str.clone()
                                cy=cy_str.clone()
                                r=r_str
                                fill=fill
                                stroke=fill
                                stroke-width="2"
                                opacity={if node.done || is_active { "1" } else { "0.4" }}
                            />
                            {check_icon}
                            {tool_badge}
                            <text
                                x=cx_str
                                y=label_y
                                text-anchor="middle"
                                fill=text_col
                                font-size="11"
                                font-family="var(--font-sans)"
                            >
                                {node_name}
                            </text>
                        </g>
                    });
                }

                view! {
                    <div style="overflow-x:auto;padding:8px 0;">
                        <svg
                            width=format!("{}", svg_w)
                            height=format!("{}", SVG_H)
                            viewBox=view_box
                            style="display:block;min-width:100%;"
                        >
                            {lines_html}
                            {circles_html}
                        </svg>
                    </div>
                }
                .into_any()
            }}
        </div>
    }
}
