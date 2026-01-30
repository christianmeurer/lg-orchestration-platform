use serde::{Deserialize, Serialize};
use serde_json::Value;

#[derive(Debug, Deserialize)]
pub struct ToolExecuteRequest {
    pub tool: String,
    #[serde(default)]
    pub input: Value,
}

#[derive(Debug, Deserialize)]
pub struct ToolBatchExecuteRequest {
    pub calls: Vec<ToolExecuteRequest>,
}

#[derive(Debug, Serialize)]
pub struct ToolBatchExecuteResponse {
    pub results: Vec<ToolEnvelope>,
}

#[derive(Debug, Serialize)]
pub struct ToolEnvelope {
    pub tool: String,
    pub ok: bool,
    pub exit_code: i32,
    pub stdout: String,
    pub stderr: String,
    pub timing_ms: u128,
    pub artifacts: Value,
}

impl ToolEnvelope {
    pub fn ok(
        tool: impl Into<String>,
        stdout: impl Into<String>,
        artifacts: Value,
        timing_ms: u128,
    ) -> Self {
        Self {
            tool: tool.into(),
            ok: true,
            exit_code: 0,
            stdout: stdout.into(),
            stderr: String::new(),
            timing_ms,
            artifacts,
        }
    }

    pub fn err(
        tool: impl Into<String>,
        exit_code: i32,
        stderr: impl Into<String>,
        artifacts: Value,
        timing_ms: u128,
    ) -> Self {
        Self {
            tool: tool.into(),
            ok: false,
            exit_code,
            stdout: String::new(),
            stderr: stderr.into(),
            timing_ms,
            artifacts,
        }
    }
}
