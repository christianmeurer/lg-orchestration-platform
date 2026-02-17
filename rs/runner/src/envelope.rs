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

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn test_envelope_ok_fields() {
        let env = ToolEnvelope::ok("read_file", "content", json!({"path": "a.txt"}), 42);
        assert_eq!(env.tool, "read_file");
        assert!(env.ok);
        assert_eq!(env.exit_code, 0);
        assert_eq!(env.stdout, "content");
        assert!(env.stderr.is_empty());
        assert_eq!(env.timing_ms, 42);
        assert_eq!(env.artifacts, json!({"path": "a.txt"}));
    }

    #[test]
    fn test_envelope_err_fields() {
        let env = ToolEnvelope::err("exec", 1, "failed", json!(null), 100);
        assert_eq!(env.tool, "exec");
        assert!(!env.ok);
        assert_eq!(env.exit_code, 1);
        assert!(env.stdout.is_empty());
        assert_eq!(env.stderr, "failed");
        assert_eq!(env.timing_ms, 100);
    }

    #[test]
    fn test_envelope_ok_serializes() {
        let env = ToolEnvelope::ok("health", "ok", json!({}), 0);
        let json_str = serde_json::to_string(&env).unwrap();
        let val: Value = serde_json::from_str(&json_str).unwrap();
        assert_eq!(val["tool"], "health");
        assert_eq!(val["ok"], true);
        assert_eq!(val["exit_code"], 0);
    }

    #[test]
    fn test_execute_request_deserializes() {
        let json_str = r#"{"tool": "read_file", "input": {"path": "README.md"}}"#;
        let req: ToolExecuteRequest = serde_json::from_str(json_str).unwrap();
        assert_eq!(req.tool, "read_file");
        assert_eq!(req.input["path"], "README.md");
    }

    #[test]
    fn test_execute_request_default_input() {
        let json_str = r#"{"tool": "health"}"#;
        let req: ToolExecuteRequest = serde_json::from_str(json_str).unwrap();
        assert_eq!(req.tool, "health");
        assert!(req.input.is_null());
    }

    #[test]
    fn test_batch_request_deserializes() {
        let json_str = r#"{"calls": [{"tool": "a"}, {"tool": "b", "input": {}}]}"#;
        let req: ToolBatchExecuteRequest = serde_json::from_str(json_str).unwrap();
        assert_eq!(req.calls.len(), 2);
        assert_eq!(req.calls[0].tool, "a");
        assert_eq!(req.calls[1].tool, "b");
    }

    #[test]
    fn test_batch_response_serializes() {
        let resp = ToolBatchExecuteResponse {
            results: vec![
                ToolEnvelope::ok("a", "ok", json!({}), 1),
                ToolEnvelope::err("b", 1, "fail", json!(null), 2),
            ],
        };
        let json_str = serde_json::to_string(&resp).unwrap();
        let val: Value = serde_json::from_str(&json_str).unwrap();
        assert_eq!(val["results"].as_array().unwrap().len(), 2);
    }
}
