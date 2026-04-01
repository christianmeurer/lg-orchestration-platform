use leptos::prelude::*;
use wasm_bindgen::{prelude::*, JsCast};
use web_sys::{EventSource, MessageEvent};

use super::types::*;

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
    pub final_output: Option<String>,
    pub is_done: bool,
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

            // Try to parse as a known SseEvent first.
            match serde_json::from_str::<SseEvent>(&data) {
                Ok(sse_event) => match sse_event {
                    SseEvent::Done => {
                        sw.update(|s| s.is_done = true);
                        es_close.close();
                    }
                    SseEvent::ToolStdout { tool, line } => {
                        sw.update(|s| s.stdout_lines.push(StdoutLine { tool, line }));
                    }
                    SseEvent::FinalOutput { text } => {
                        sw.update(|s| s.final_output = Some(text));
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
                    }
                    SseEvent::Unknown => {
                        // Fall through to TraceEvent parsing.
                        if let Ok(trace) = serde_json::from_str::<TraceEvent>(&data) {
                            sw.update(|s| s.events.push(trace));
                        }
                    }
                },
                Err(_) => {
                    // Not a recognised SseEvent — try as a raw TraceEvent.
                    if let Ok(trace) = serde_json::from_str::<TraceEvent>(&data) {
                        sw.update(|s| s.events.push(trace));
                    }
                }
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
