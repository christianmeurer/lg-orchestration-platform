use leptos::prelude::*;
use wasm_bindgen::{prelude::*, JsCast};
use web_sys::{EventSource, MessageEvent};

use super::types::*;

/// Run summary sent by the SSE stream (not a trace event).
/// The server polls the run and sends its full state each time.
#[derive(Debug, Clone, serde::Deserialize)]
struct RunSummaryEvent {
    #[serde(default)]
    run_id: Option<String>,
    #[serde(default)]
    status: Option<String>,
    #[serde(default)]
    request: Option<String>,
    #[serde(default)]
    new_log_lines: Vec<String>,
    #[serde(default)]
    pending_approval: bool,
    #[serde(default)]
    pending_approval_summary: Option<String>,
    #[serde(default)]
    trace: Option<serde_json::Value>,
    #[serde(default)]
    _current_node: Option<String>,
}

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

#[derive(Debug, Clone)]
pub struct StdoutLine {
    pub tool: String,
    pub line: String,
}

#[derive(Debug, Clone, Default)]
pub struct RunState {
    pub events: Vec<TraceEvent>,
    pub stdout_lines: Vec<StdoutLine>,
    pub log_lines: Vec<String>,
    pub final_output: Option<String>,
    pub is_done: bool,
    pub status: Option<String>,
    pub request: Option<String>,
    pub approval: Option<ApprovalRequest>,
}

// ---------------------------------------------------------------------------
// connect_sse
// ---------------------------------------------------------------------------

/// Open an SSE stream for `run_id` and return a read signal that is updated as
/// events arrive, together with a cleanup closure that closes the `EventSource`.
///
/// The URL used is:
///   `{base_url}/v1/runs/{run_id}/stream?access_token=<token>`
pub fn connect_sse(
    base_url: &str,
    run_id: &str,
    token: Option<String>,
) -> (ReadSignal<RunState>, impl Fn()) {
    let (state_read, state_write) = signal(RunState::default());
    let run_id = run_id.to_owned();

    // Build the URL, appending the token as a query parameter when present.
    let url = match token {
        Some(ref tok) => format!("{}/v1/runs/{}/stream?access_token={}", base_url, run_id, tok),
        None => format!("{}/v1/runs/{}/stream", base_url, run_id),
    };
    let run_id_owned = run_id.clone();

    let es = EventSource::new(&url).expect("EventSource::new failed");

    // --- onmessage -----------------------------------------------------------
    {
        let es_close = es.clone();
        let sw = state_write;

        let on_message = Closure::<dyn FnMut(MessageEvent)>::new(move |event: MessageEvent| {
            let data = match event.data().as_string() {
                Some(s) => s,
                None => return,
            };

            // 1. Check for done sentinel
            if let Ok(sse) = serde_json::from_str::<SseEvent>(&data) {
                match sse {
                    SseEvent::Done => {
                        sw.update(|s| s.is_done = true);
                        es_close.close();
                        return;
                    }
                    SseEvent::ToolStdout { tool, line } => {
                        sw.update(|s| s.stdout_lines.push(StdoutLine { tool, line }));
                        return;
                    }
                    SseEvent::FinalOutput { text } => {
                        sw.update(|s| s.final_output = Some(text));
                        return;
                    }
                    SseEvent::ApprovalRequested { challenge_id, summary, operation_class } => {
                        let rid = run_id_owned.clone();
                        sw.update(move |s| {
                            s.approval = Some(ApprovalRequest {
                                run_id: rid,
                                challenge_id,
                                summary,
                                operation_class,
                            });
                        });
                        return;
                    }
                    SseEvent::Unknown => {}
                }
            }

            // 2. Check for run summary (the server sends full run state each poll)
            if let Ok(summary) = serde_json::from_str::<RunSummaryEvent>(&data) {
                if summary.run_id.is_some() {
                    sw.update(|s| {
                        // Update status
                        if let Some(ref st) = summary.status {
                            s.status = Some(st.clone());
                            if st == "succeeded" || st == "failed" || st == "cancelled" {
                                s.is_done = true;
                            }
                        }
                        s.request = summary.request.clone().or_else(|| s.request.clone());

                        // Append new log lines
                        for line in &summary.new_log_lines {
                            if !line.is_empty() && !s.log_lines.contains(line) {
                                s.log_lines.push(line.clone());
                            }
                        }

                        // Check for approval
                        if summary.pending_approval {
                            s.approval = Some(ApprovalRequest {
                                run_id: summary.run_id.clone().unwrap_or_default(),
                                challenge_id: None,
                                summary: summary.pending_approval_summary.clone(),
                                operation_class: None,
                            });
                        }

                        // Extract trace events if present
                        if let Some(ref trace_val) = summary.trace {
                            if let Some(events) = trace_val.get("events") {
                                if let Ok(trace_events) =
                                    serde_json::from_value::<Vec<TraceEvent>>(events.clone())
                                {
                                    if trace_events.len() > s.events.len() {
                                        s.events = trace_events;
                                    }
                                }
                            }
                        }
                    });
                    if summary.status.as_deref() == Some("succeeded")
                        || summary.status.as_deref() == Some("failed")
                    {
                        es_close.close();
                    }
                    return;
                }
            }

            // 3. Fall back to raw TraceEvent
            if let Ok(trace) = serde_json::from_str::<TraceEvent>(&data) {
                sw.update(|s| s.events.push(trace));
            }
        });

        es.set_onmessage(Some(on_message.as_ref().unchecked_ref()));
        on_message.forget();
    }

    // --- onerror -------------------------------------------------------------
    {
        let es_close = es.clone();
        let sw = state_write;

        let on_error = Closure::<dyn FnMut(web_sys::Event)>::new(move |_event: web_sys::Event| {
            sw.update(|s| s.is_done = true);
            es_close.close();
        });

        es.set_onerror(Some(on_error.as_ref().unchecked_ref()));
        on_error.forget();
    }

    // --- cleanup closure -----------------------------------------------------
    let cleanup = {
        let es_cleanup = es;
        move || {
            es_cleanup.close();
        }
    };

    (state_read, cleanup)
}
