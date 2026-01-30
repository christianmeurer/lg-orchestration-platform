use std::time::Instant;

use serde_json::{json, Value};

use crate::config::RunnerConfig;
use crate::envelope::{ToolEnvelope, ToolExecuteRequest};
use crate::errors::ApiError;

mod exec;
mod fs;

pub async fn dispatch_tool(
    cfg: &RunnerConfig,
    req: ToolExecuteRequest,
) -> Result<ToolEnvelope, ApiError> {
    let started = Instant::now();
    let tool = req.tool.trim().to_string();
    let out = match tool.as_str() {
        "health" => Ok(ToolEnvelope::ok(
            "health",
            "ok",
            json!({}),
            started.elapsed().as_millis(),
        )),
        "read_file" => fs::read_file(cfg, req.input).await,
        "list_files" => fs::list_files(cfg, req.input).await,
        "apply_patch" => fs::apply_patch(cfg, req.input).await,
        "exec" => exec::exec(cfg, req.input).await,
        other => Err(ApiError::BadRequest(format!("unknown tool: {other}"))),
    };
    match out {
        Ok(mut env) => {
            env.timing_ms = started.elapsed().as_millis();
            Ok(env)
        }
        Err(e) => {
            let mut env = ToolEnvelope::err(
                tool,
                1,
                e.to_string(),
                Value::Null,
                started.elapsed().as_millis(),
            );
            env.timing_ms = started.elapsed().as_millis();
            Ok(env)
        }
    }
}
